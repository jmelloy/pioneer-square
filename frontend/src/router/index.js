import { createRouter, createWebHistory } from 'vue-router'
import LandingView from '../views/LandingView.vue'
import AppView from '../views/AppView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/',
      component: LandingView,
    },
    {
      path: '/:sessionId',
      component: AppView,
      props: true,
    },
  ],
})

export default router
