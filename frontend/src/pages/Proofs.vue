<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'

const rows = ref([])
const capProof = ref(null)
const capCurrent = ref([])
const capAudits = ref([])
const err = ref('')

onMounted(async () => {
  try {
    ;[rows.value, capProof.value, capCurrent.value, capAudits.value] = await Promise.all([
      api.proofs(),
      api.openCapProof(),
      api.openCapCurrent(),
      api.openCapAudits(),
    ])
  } catch (e) {
    err.value = String(e.message || e)
  }
})
</script>

<template>
  <!-- plant-ui -->

  <section class="panel">
    <h1>open 唯一核对</h1>
    <p class="muted">
      {{ capProof?.constraint }}；{{ capProof?.path }}。任一访客同一时刻至多一段 open。
    </p>
    <p v-if="capProof" :class="capProof.ok ? 'ok' : 'err'">
      {{ capProof.ok ? '✓ 核对通过' : '✗ 核对未过' }}：
      闸 {{ capProof.gate_rows }} 行 / open {{ capProof.open_sessions }} 段 /
      关闭审计 {{ capProof.close_audits }} 条
      <template v-if="!capProof.ok">
        — 多 open 访客 {{ capProof.multi_open_visitors.length }}、
        无闸 open {{ capProof.ungated_open_sessions.length }}、
        悬空闸 {{ capProof.dangling_gate_rows.length }}
      </template>
    </p>
    <p v-if="err" class="err">{{ err }}</p>

    <h2>现行闸</h2>
    <table class="data">
      <thead>
        <tr><th>访客</th><th>现行 open 会话</th><th>开闸时刻</th><th>点数</th></tr>
      </thead>
      <tbody>
        <tr v-for="r in capCurrent" :key="r.visitor_id">
          <td>{{ r.visitor_id }}</td>
          <td class="mono">{{ r.session_id }}</td>
          <td class="mono">{{ r.opened_at }}</td>
          <td>{{ r.hit_count }}</td>
        </tr>
      </tbody>
    </table>

    <h2>关闭审计</h2>
    <table class="data">
      <thead>
        <tr><th>被关会话</th><th>访客</th><th>原因</th><th>承接会话</th><th>关闭时刻</th></tr>
      </thead>
      <tbody>
        <tr v-for="a in capAudits" :key="a.id">
          <td class="mono">{{ a.session_id }}</td>
          <td>{{ a.visitor_id }}</td>
          <td>{{ a.reason }}</td>
          <td class="mono">{{ a.successor_session_id || '—' }}</td>
          <td class="mono">{{ a.created_at }}</td>
        </tr>
      </tbody>
    </table>
  </section>

  <section class="panel">
    <h1>会话核对</h1>
    <p class="muted">会话击点集合与起止须与织机汇总可核。</p>
    <table class="data">
      <thead>
        <tr><th>会话</th><th>访客</th><th>点数</th><th>起止</th><th>击点集</th></tr>
      </thead>
      <tbody>
        <tr v-for="r in rows" :key="r.id">
          <td class="mono">{{ r.session_id }}</td>
          <td>{{ r.visitor_id }}</td>
          <td>{{ r.hit_count }}</td>
          <td class="mono">{{ r.started_at }} → {{ r.ended_at }}</td>
          <td class="mono">{{ r.hit_ids_json }}</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>
