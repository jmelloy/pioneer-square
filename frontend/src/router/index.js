import { createRouter, createWebHistory } from 'vue-router'
import AppView from '../views/AppView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    {
      path: '/',
      redirect: () => {
        return { path: '/new' }
      }
    },
    {
      path: '/new',
      component: AppView
    },
    {
      path: '/:sessionId',
      component: AppView,
      props: true
    }
  ]
})

export default router
