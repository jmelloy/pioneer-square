import { ref } from 'vue'

export function useWebRTC(sendSignal) {
  const peerConnections = ref({})
  const dataChannels = ref({})

  const config = {
    iceServers: [{ urls: 'stun:stun.l.google.com:19302' }]
  }

  function createPeerConnection(peerId) {
    const pc = new RTCPeerConnection(config)
    peerConnections.value[peerId] = pc

    pc.onicecandidate = (event) => {
      if (event.candidate) {
        sendSignal({ type: 'ice-candidate', to: peerId, candidate: event.candidate })
      }
    }

    pc.ondatachannel = (event) => {
      const channel = event.channel
      channel.onmessage = (e) => {
        console.log('DataChannel message from', peerId, e.data)
      }
      dataChannels.value[peerId] = channel
    }

    return pc
  }

  async function createOffer(peerId) {
    const pc = createPeerConnection(peerId)
    const channel = pc.createDataChannel('agent-comm')
    dataChannels.value[peerId] = channel
    const offer = await pc.createOffer()
    await pc.setLocalDescription(offer)
    sendSignal({ type: 'offer', to: peerId, offer })
    return pc
  }

  async function handleOffer(peerId, offer) {
    const pc = createPeerConnection(peerId)
    await pc.setRemoteDescription(new RTCSessionDescription(offer))
    const answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)
    sendSignal({ type: 'answer', to: peerId, answer })
  }

  async function handleAnswer(peerId, answer) {
    const pc = peerConnections.value[peerId]
    if (pc) await pc.setRemoteDescription(new RTCSessionDescription(answer))
  }

  async function handleIceCandidate(peerId, candidate) {
    const pc = peerConnections.value[peerId]
    if (pc) await pc.addIceCandidate(new RTCIceCandidate(candidate))
  }

  function sendDataChannelMessage(peerId, message) {
    const channel = dataChannels.value[peerId]
    if (channel && channel.readyState === 'open') {
      channel.send(JSON.stringify(message))
    }
  }

  return {
    peerConnections,
    dataChannels,
    createOffer,
    handleOffer,
    handleAnswer,
    handleIceCandidate,
    sendDataChannelMessage
  }
}
