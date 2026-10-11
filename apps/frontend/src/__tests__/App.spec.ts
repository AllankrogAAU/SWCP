import { describe, it, expect, vi } from 'vitest'

import { mount } from '@vue/test-utils'
import App from '../App.vue'

vi.mock('monaco-editor-vue3', () => ({
  CodeEditor: {
    template: '<div data-testid="code-editor" />',
  },
}))

describe('App', () => {
  it('mounts renders properly', () => {
    const wrapper = mount(App)
    expect(wrapper.text()).toContain('Hello, World')
  })
})
