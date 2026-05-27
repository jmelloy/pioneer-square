/**
 * Typed wrapper around the iOS WKWebView bridge. Safe to import from any
 * frontend code — on the web it's a set of no-ops, in the iOS shell it
 * proxies to `window.webkit.messageHandlers.pioneerSquare`.
 *
 * Native side lives at `ios/PioneerSquare/NativeBridge.swift`.
 */

type NativeMessage =
  | { type: 'requestPushPermission' }
  | { type: 'openExternal'; url: string }
  | { type: 'log'; message: string }

interface PioneerSquareNative {
  platform: 'ios'
  send: (msg: NativeMessage) => void
  onPushToken?: (payload: { token: string }) => void
  onNotificationTap?: (payload: Record<string, unknown>) => void
}

declare global {
  interface Window {
    pioneerSquareNative?: PioneerSquareNative
  }
}

export function isNative(): boolean {
  return typeof window !== 'undefined' && !!window.pioneerSquareNative
}

export function nativePlatform(): 'ios' | 'web' {
  return window.pioneerSquareNative?.platform ?? 'web'
}

export function requestPushPermission(): void {
  window.pioneerSquareNative?.send({ type: 'requestPushPermission' })
}

export function openExternal(url: string): void {
  if (window.pioneerSquareNative) {
    window.pioneerSquareNative.send({ type: 'openExternal', url })
  } else {
    window.open(url, '_blank', 'noopener,noreferrer')
  }
}

type PushTokenHandler = (token: string) => void
type NotificationTapHandler = (payload: Record<string, unknown>) => void

/**
 * Register a callback that fires when APNs hands the native shell a new
 * device token. Returns an unsubscribe function. On the web the callback
 * is never invoked.
 */
export function onPushToken(handler: PushTokenHandler): () => void {
  const native = window.pioneerSquareNative
  if (!native) return () => {}
  const previous = native.onPushToken
  native.onPushToken = (payload) => {
    previous?.(payload)
    handler(payload.token)
  }
  return () => {
    if (native.onPushToken && native.onPushToken === handler) {
      native.onPushToken = previous
    }
  }
}

/**
 * Register a callback for APNs notification taps. The payload is the
 * notification's `userInfo` dictionary — by convention we put a `taskId`
 * or `guildId` there so the frontend can route the user.
 */
export function onNotificationTap(handler: NotificationTapHandler): () => void {
  const native = window.pioneerSquareNative
  if (!native) return () => {}
  const previous = native.onNotificationTap
  native.onNotificationTap = (payload) => {
    previous?.(payload)
    handler(payload)
  }
  return () => {
    if (native.onNotificationTap && native.onNotificationTap === handler) {
      native.onNotificationTap = previous
    }
  }
}
