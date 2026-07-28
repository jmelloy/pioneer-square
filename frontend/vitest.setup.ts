// Node 22+ exposes a native global `localStorage` that is `undefined` unless
// `--localstorage-file` is passed. Because the binding merely *exists*, Vitest's
// happy-dom setup skips copying happy-dom's real localStorage onto the global
// (it only overrides keys absent from the global). The result: `localStorage`
// resolves to Node's empty native one and every storage access throws.
//
// Install a simple Map-backed Storage so tests get working web storage
// regardless of the Node/happy-dom interaction.
class MemoryStorage implements Storage {
  private store = new Map<string, string>()
  get length() {
    return this.store.size
  }
  clear() {
    this.store.clear()
  }
  getItem(key: string) {
    return this.store.has(key) ? this.store.get(key)! : null
  }
  key(index: number) {
    return [...this.store.keys()][index] ?? null
  }
  removeItem(key: string) {
    this.store.delete(key)
  }
  setItem(key: string, value: string) {
    this.store.set(key, String(value))
  }
}

for (const name of ['localStorage', 'sessionStorage'] as const) {
  Object.defineProperty(globalThis, name, {
    value: new MemoryStorage(),
    configurable: true,
    writable: true,
  })
}
