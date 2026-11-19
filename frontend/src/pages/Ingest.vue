<script setup>
import { ref } from 'vue'
import { api } from '../api.js'

const result = ref(null)
const err = ref('')
const busy = ref(false)

async function run(fixture) {
  busy.value = true
  err.value = ''
  result.value = null
  try {
    result.value = await api.ingest(fixture)
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}
</script>

<template>
  <!-- plant-ui -->

  <section class="panel">
    <h1>收流击点</h1>
    <p class="muted">读取夹具灌入正式通道；同访客单 open 由 visitor_open 闸表强制，强制新会话装载后关旧开新。</p>
    <button class="btn" :disabled="busy" @click="run()">{{ busy ? '收流中…' : '跑一批收流' }}</button>
    <button class="btn" :disabled="busy" @click="run('single_open_cap_batch.json')">
      {{ busy ? '收流中…' : '跑双 open 风险夹具' }}
    </button>
    <p v-if="err" class="err">{{ err }}</p>
    <pre v-if="result" class="mono">{{ result }}</pre>
  </section>
</template>
