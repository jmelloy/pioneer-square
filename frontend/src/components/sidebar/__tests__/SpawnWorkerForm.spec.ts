import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createTestingPinia } from '@pinia/testing'
import SpawnWorkerForm from '../SpawnWorkerForm.vue'
import { SETTINGS_KEY as makeSettingsKey } from '../spawnWorkerFormConstants'

vi.mock('../../../utils/api', () => ({
  api: vi.fn().mockResolvedValue({ worker_id: 'test-worker' }),
}))

const GUILD_ID = 'guild-1'
const SETTINGS_KEY = makeSettingsKey(GUILD_ID)
const TEST_REPOS = [{ full_name: 'owner/repo', language: 'TypeScript' }]

function createWrapper() {
  return mount(SpawnWorkerForm, {
    global: {
      plugins: [
        createTestingPinia({
          initialState: {
            guild: { currentGuild: { id: GUILD_ID, name: 'Test Guild' } },
            github: {
              repos: TEST_REPOS,
              selectedRepos: ['owner/repo'],
              token: 'gh-token',
            },
          },
        }),
      ],
    },
  })
}

describe('SpawnWorkerForm env var localStorage safety', () => {
  beforeEach(() => {
    localStorage.clear()
    vi.clearAllMocks()
  })

  afterEach(() => {
    localStorage.clear()
  })

  describe('saveSettings', () => {
    it('stores env var keys with empty values, not the actual secret values', async () => {
      const wrapper = createWrapper()
      await flushPromises()

      await wrapper.find('[data-testid="spawn-env-add"]').trigger('click')
      await wrapper.vm.$nextTick()

      await wrapper.find('[data-testid="spawn-env-key"]').setValue('MY_SECRET')
      await wrapper.find('[data-testid="spawn-env-val"]').setValue('super-secret-value')

      // Clicking the launch button triggers saveSettings as a side effect.
      await wrapper.find('[data-testid="spawn-launch-btn"]').trigger('click')
      await flushPromises()

      const raw = localStorage.getItem(SETTINGS_KEY)
      expect(raw).not.toBeNull()
      const saved = JSON.parse(raw!)
      expect(saved.envVars).toEqual([{ key: 'MY_SECRET', value: '' }])
    })

    it('does not save entries with blank keys', async () => {
      const wrapper = createWrapper()
      await flushPromises()

      await wrapper.find('[data-testid="spawn-env-add"]').trigger('click')
      await wrapper.find('[data-testid="spawn-env-add"]').trigger('click')
      await flushPromises()

      const keyInputs = wrapper.findAll('[data-testid="spawn-env-key"]')
      const valInputs = wrapper.findAll('[data-testid="spawn-env-val"]')
      await keyInputs[0].setValue('VALID_KEY')
      await valInputs[0].setValue('some-value')
      // leave keyInputs[1] blank

      // Clicking the launch button triggers saveSettings as a side effect.
      await wrapper.find('[data-testid="spawn-launch-btn"]').trigger('click')
      await flushPromises()

      const raw = localStorage.getItem(SETTINGS_KEY)
      expect(raw).not.toBeNull()
      const saved = JSON.parse(raw!)
      expect(saved.envVars).toEqual([{ key: 'VALID_KEY', value: '' }])
    })
  })

  describe('loadSavedSettings', () => {
    it('restores keys with empty values even when stored data contains non-empty values', async () => {
      localStorage.setItem(
        SETTINGS_KEY,
        JSON.stringify({
          repos: ['owner/repo'],
          tools: [],
          envVars: [{ key: 'MY_VAR', value: 'stale-secret' }],
        }),
      )

      const wrapper = createWrapper()
      await flushPromises()

      const keyInput = wrapper.find('[data-testid="spawn-env-key"]').element as HTMLInputElement
      const valInput = wrapper.find('[data-testid="spawn-env-val"]').element as HTMLInputElement
      expect(keyInput.value).toBe('MY_VAR')
      expect(valInput.value).toBe('')
    })

    it.each(['null', '42', '"a string"', 'true', '[]'])(
      'returns null and renders defaults when localStorage contains %s',
      async (bad) => {
        localStorage.setItem(SETTINGS_KEY, bad)
        const wrapper = createWrapper()
        await flushPromises()
        // Should fall back to default (selected repos from store, no env vars)
        expect(wrapper.findAll('[data-testid="spawn-env-key"]')).toHaveLength(0)
        wrapper.unmount()
      },
    )

    it('returns null and renders defaults when localStorage contains malformed JSON', async () => {
      localStorage.setItem(SETTINGS_KEY, '{not valid json}')
      const wrapper = createWrapper()
      await flushPromises()
      expect(wrapper.findAll('[data-testid="spawn-env-key"]')).toHaveLength(0)
    })

    it('restores multiple keys all with empty values', async () => {
      localStorage.setItem(
        SETTINGS_KEY,
        JSON.stringify({
          repos: ['owner/repo'],
          tools: [],
          envVars: [
            { key: 'FOO', value: 'foo-secret' },
            { key: 'BAR', value: 'bar-secret' },
          ],
        }),
      )

      const wrapper = createWrapper()
      await flushPromises()

      const valInputs = wrapper.findAll('[data-testid="spawn-env-val"]')
      expect(valInputs).toHaveLength(2)
      for (const input of valInputs) {
        expect((input.element as HTMLInputElement).value).toBe('')
      }
    })
  })
})
