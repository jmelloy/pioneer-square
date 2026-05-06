import { createRouter, createWebHistory } from 'vue-router'
import LandingView from '../views/LandingView.vue'
import AppView from '../views/AppView.vue'

export function getRootOrigin(): string | null {
  const hostname = window.location.hostname
  const port = window.location.port
  const scheme = window.location.protocol
  if (hostname.endsWith('.pioneer-square.melloy.life')) {
    return `${scheme}//pioneer-square.melloy.life`
  }
  if (hostname !== 'localhost' && hostname.endsWith('.localhost')) {
    return `${scheme}//localhost${port ? ':' + port : ''}`
  }
  return null
}

function getSubdomainGuild(): string | null {
  const hostname = window.location.hostname
  for (const base of ['pioneer-square.melloy.life', 'localhost']) {
    const suffix = `.${base}`
    if (hostname.endsWith(suffix)) {
      return hostname.slice(0, -suffix.length)
    }
  }
  return null
}

const subdomainGuild = getSubdomainGuild()

const router = createRouter({
  history: createWebHistory(),
  routes: subdomainGuild
    ? [
        {
          path: '/:pathMatch(.*)*',
          component: AppView,
          props: () => ({ guildId: subdomainGuild }),
        },
      ]
    : [
        {
          path: '/',
          component: LandingView,
        },
        {
          path: '/:guildId',
          component: AppView,
          props: true,
        },
      ],
})

export default router
