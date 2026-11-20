<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'

const proof = ref(null)
const switches = ref([])
const err = ref('')

onMounted(async () => {
  try {
    ;[proof.value, switches.value] = await Promise.all([
      api.openCapSwitchProof(),
      api.openCapSwitches(),
    ])
  } catch (e) {
    err.value = String(e.message || e)
  }
})
</script>

<template>
  <!-- plant-ui -->

  <section class="panel">
    <h1>切换核证</h1>
    <p class="muted">
      唯一切换已发生，切换审计必须落地：每条旧→新边含访客、旧/新会话、关闭审计键与时刻，
      旧会话写闸同步关闭，指针与审计同成败。
    </p>
    <p v-if="proof" :class="proof.ok ? 'ok' : 'err'">
      {{ proof.ok ? '✓ 核证通过' : '✗ 核证未过' }}：{{ proof.conclusion }}
      （切换边 {{ proof.edge_count }} 条 · {{ proof.storage }}）
    </p>
    <p v-if="err" class="err">{{ err }}</p>

    <h2>切换审计边（旧 → 新）</h2>
    <table class="data">
      <thead>
        <tr>
          <th>访客</th>
          <th>旧会话（写闸态）</th>
          <th>新会话（状态）</th>
          <th>关闭审计键</th>
          <th>切换时刻</th>
          <th>闸指新会话</th>
          <th>结论</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="e in proof?.edges || []" :key="e.edge_id">
          <td>{{ e.visitor_id }}</td>
          <td class="mono">{{ e.old_session_id }} <em>[{{ e.old_write_gate }}]</em></td>
          <td class="mono">{{ e.new_session_id }} <em>[{{ e.new_status }}]</em></td>
          <td class="mono">#{{ e.close_audit_id }}</td>
          <td class="mono">{{ e.switched_at }}</td>
          <td>{{ e.gate_points_at_new ? '是' : '已被下一跳替换' }}</td>
          <td :class="e.verdict === '通过' ? 'ok' : 'err'">
            {{ e.verdict }}
            <template v-if="e.violations.length">（{{ e.violations.join('；') }}）</template>
          </td>
        </tr>
        <tr v-if="!(proof?.edges || []).length">
          <td colspan="7" class="muted">尚无切换边（首访立闸不产生切换）。</td>
        </tr>
      </tbody>
    </table>

    <h2>违规明细</h2>
    <table class="data" v-if="proof?.violations?.length">
      <thead>
        <tr><th>对象</th><th>问题</th></tr>
      </thead>
      <tbody>
        <tr v-for="(v, i) in proof.violations" :key="i">
          <td class="mono">
            {{ v.edge_id ? `边#${v.edge_id}` : v.close_audit_id ? `审计#${v.close_audit_id}` : v.visitor_id || v.old_session_id }}
          </td>
          <td class="err">{{ v.problems.join('；') }}</td>
        </tr>
      </tbody>
    </table>
    <p v-else class="ok">无违规：不存在切换成功而审计边缺行、旧会话切换后仍可追加、指针已新而审计仍空。</p>

    <h2>切换边原始记录</h2>
    <table class="data">
      <thead>
        <tr><th>id</th><th>访客</th><th>旧会话</th><th>新会话</th><th>关闭审计键</th><th>时刻</th></tr>
      </thead>
      <tbody>
        <tr v-for="s in switches" :key="s.id">
          <td class="mono">{{ s.id }}</td>
          <td>{{ s.visitor_id }}</td>
          <td class="mono">{{ s.old_session_id }}</td>
          <td class="mono">{{ s.new_session_id }}</td>
          <td class="mono">#{{ s.close_audit_id }}</td>
          <td class="mono">{{ s.switched_at }}</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>
