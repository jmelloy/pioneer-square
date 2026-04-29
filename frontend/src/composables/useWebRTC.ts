import { ref } from 'vue'

type SignalPayload = Record<string, any>

export function useWebRTC(sendSignal: (payload: SignalPayload) => void) {
  const peerConnections = ref<Record<string, RTCPeerConnection>>({})
  const dataChannels = ref<Record<string, RTCDataChannel>>({})

  const config: RTCConfiguration = {
    iceServers: [{ urls: 'stun:stun.l.google.com:19302' }]
  }

  function createPeerConnection(peerId: string) {
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

  async function createOffer(peerId: string) {
    const pc = createPeerConnection(peerId)
    const channel = pc.createDataChannel('agent-comm')
    dataChannels.value[peerId] = channel
    const offer = await pc.createOffer()
    await pc.setLocalDescription(offer)
    sendSignal({ type: 'offer', to: peerId, offer })
    return pc
  }

  async function handleOffer(peerId: string, offer: RTCSessionDescriptionInit) {
    const pc = createPeerConnection(peerId)
    await pc.setRemoteDescription(new RTCSessionDescription(offer))
    const answer = await pc.createAnswer()
    await pc.setLocalDescription(answer)
    sendSignal({ type: 'answer', to: peerId, answer })
  }

  async function handleAnswer(peerId: string, answer: RTCSessionDescriptionInit) {
    const pc = peerConnections.value[peerId]
    if (pc) await pc.setRemoteDescription(new RTCSessionDescription(answer))
  }

  async function handleIceCandidate(peerId: string, candidate: RTCIceCandidateInit) {
    const pc = peerConnections.value[peerId]
    if (pc) await pc.addIceCandidate(new RTCIceCandidate(candidate))
  }

  function sendDataChannelMessage(peerId: string, message: any) {
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
