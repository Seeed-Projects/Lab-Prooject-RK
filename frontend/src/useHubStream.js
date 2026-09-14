import { onBeforeUnmount, ref } from 'vue'
import { websocketUrl } from './api'

const MAX_EVENTS = 300

export function useHubStream(onEvent) {
  const status = ref('disconnected')
  const lastError = ref('')
  let socket = null
  let retryTimer = null
  let stopped = false
  let retryCount = 0

  function connect() {
    clearTimeout(retryTimer)
    status.value = 'connecting'
    socket = new WebSocket(websocketUrl())

    socket.addEventListener('open', () => {
      retryCount = 0
      status.value = 'connected'
      lastError.value = ''
      socket.send(JSON.stringify({
        action: 'subscribe',
        demo_ids: [],
        run_ids: [],
        channels: ['status', 'data', 'metrics', 'log', 'artifact'],
        after_seq: 0,
      }))
    })

    socket.addEventListener('message', (message) => {
      try {
        const event = JSON.parse(message.data)
        if (event.type !== 'hub.subscribed') onEvent(event, MAX_EVENTS)
      } catch {
        lastError.value = 'Received an unreadable event'
      }
    })

    socket.addEventListener('error', () => {
      lastError.value = 'Live connection unavailable'
    })

    socket.addEventListener('close', () => {
      status.value = 'disconnected'
      if (!stopped) {
        const delay = Math.min(1000 * 2 ** retryCount, 15000)
        retryCount += 1
        retryTimer = setTimeout(connect, delay)
      }
    })
  }

  function disconnect() {
    stopped = true
    clearTimeout(retryTimer)
    socket?.close()
  }

  onBeforeUnmount(disconnect)
  return { status, lastError, connect, disconnect }
}
