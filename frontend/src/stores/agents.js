import { defineStore } from 'pinia'
import { ref } from 'vue'
import { useSessionStore } from './session'

export const useAgentsStore = defineStore('agents', () => {
  const agents = ref([])

  function registerAgent(agentData) {
    const existing = agents.value.find(a => a.id === agentData.agentId)
    if (existing) {
      existing.state = agentData.state || 'idle'
      existing.name = agentData.agentName || existing.name
    } else {
      agents.value.push({
        id: agentData.agentId,
        name: agentData.agentName || 'Unknown',
        type: agentData.agentType || 'worker',
        state: agentData.state || 'idle',
        logs: [],
        joinedAt: agentData.joinedAt || new Date().toISOString()
      })
    }
  }

  function updateAgentState(agentId, state) {
    const agent = agents.value.find(a => a.id === agentId)
    if (agent) agent.state = state
  }

  function addLog(agentId, line, timestamp) {
    const agent = agents.value.find(a => a.id === agentId)
    if (agent) {
      agent.logs.push({ line, timestamp: timestamp || new Date().toISOString() })
      if (agent.logs.length > 500) agent.logs.shift()
    }
  }

  function sendMessage(agentId, content) {
    const sessionStore = useSessionStore()
    sessionStore.sendMessage({
      type: 'chat',
      from: 'user',
      to: agentId,
      content
    })
  }

  function handleWebSocketMessage(data) {
    if (data.type === 'agent-joined') {
      registerAgent(data)
    } else if (data.type === 'agent-state') {
      updateAgentState(data.agentId, data.state)
    } else if (data.type === 'terminal-output') {
      addLog(data.agentId, data.line, data.timestamp)
    }
  }

  return {
    agents,
    registerAgent,
    updateAgentState,
    addLog,
    sendMessage,
    handleWebSocketMessage
  }
})
