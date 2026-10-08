import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { flushPromises } from '@vue/test-utils'
import App from '../App.vue'

const api = vi.hoisted(() => ({
  clearAccessToken: vi.fn(),
  getAccessToken: vi.fn(),
  getAssignments: vi.fn(),
  getSubmission: vi.fn(),
  login: vi.fn(),
  openSubmissionSocket: vi.fn(),
  register: vi.fn(),
  setAccessToken: vi.fn(),
  submitSource: vi.fn(),
  requestHint: vi.fn(),
  runSource: vi.fn(),
}))

vi.mock('../services/api', () => api)

vi.mock('../Components/CodeEditorBox.vue', () => ({
  default: {
    props: ['busyAction', 'terminalOutput'],
    emits: ['submit', 'hint', 'run'],
    template: '<div><button data-test="submit" :disabled="busyAction !== null" @click="$emit(\'submit\')">Submit</button><button data-test="hint" :disabled="busyAction !== null" @click="$emit(\'hint\')">Hint</button><button data-test="run" :disabled="busyAction !== null" @click="$emit(\'run\')">Run</button><pre>{{ terminalOutput }}</pre></div>',
  },
}))

describe('App', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    api.getAccessToken.mockReturnValue(null)
    api.register.mockResolvedValue({ id: 'user-id' })
    api.login.mockResolvedValue({ accessToken: 'test-token', tokenType: 'Bearer' })
    api.getAssignments.mockResolvedValue([
      { id: 'assignment-id', title: 'Hello C', description: 'Print a greeting.' },
    ])
    api.submitSource.mockResolvedValue({ submissionId: 'submission-id', status: 'SANDBOX_QUEUED' })
    api.requestHint.mockRejectedValue(new Error('The hint action is not available yet.'))
    api.runSource.mockRejectedValue(new Error('The run action is not available yet.'))
    api.getSubmission.mockResolvedValue({
      submissionId: 'submission-id',
      status: 'COMPLETED',
      llmFeedback: 'Looks good.',
      sandboxOutput: null,
      errorMessage: null,
    })
    api.openSubmissionSocket.mockReturnValue({
      close: vi.fn(),
      addEventListener: vi.fn(),
    })
  })

  it('shows the login and registration entry point when unauthenticated', () => {
    const wrapper = mount(App)
    expect(wrapper.text()).toContain('SWCP code feedback')
    expect(wrapper.text()).toContain('Sign in')
    expect(wrapper.text()).toContain('Register')
  })

  it('registers, loads assignments, submits, and displays the polled result', async () => {
    const wrapper = mount(App)
    await wrapper.findAll('v-btn')[1]!.trigger('click')
    await wrapper.find('v-form').trigger('submit')
    await flushPromises()

    expect(api.register).toHaveBeenCalledOnce()
    expect(api.login).toHaveBeenCalledOnce()
    expect(wrapper.text()).toContain('Username')
    expect(wrapper.text()).toContain('Hello C')

    await wrapper.get('[data-test="submit"]').trigger('click')
    await flushPromises()

    expect(api.submitSource).toHaveBeenCalledWith('assignment-id', expect.any(String), 'azure')
    expect(api.openSubmissionSocket).toHaveBeenCalledWith('submission-id', expect.any(Function), expect.any(Function))
    expect(api.getSubmission).toHaveBeenCalledWith('submission-id')
    expect(wrapper.text()).toContain('Looks good.')
  })

  it('calls the Core hint and run endpoints and presents their current status', async () => {
    const wrapper = mount(App)
    await wrapper.find('v-form').trigger('submit')
    await flushPromises()

    await wrapper.get('[data-test="hint"]').trigger('click')
    await flushPromises()
    expect(api.requestHint).toHaveBeenCalledWith('assignment-id', expect.any(String))
    expect(wrapper.text()).toContain('hint action is not available yet')

    await wrapper.get('[data-test="run"]').trigger('click')
    await flushPromises()
    expect(api.runSource).toHaveBeenCalledWith('assignment-id', expect.any(String))
    expect(wrapper.text()).toContain('run action is not available yet')
  })

  it('disables all code actions while a submission request is pending', async () => {
    const wrapper = mount(App)
    await wrapper.find('v-form').trigger('submit')
    await flushPromises()
    api.submitSource.mockReturnValue(new Promise(() => {}))

    await wrapper.get('[data-test="submit"]').trigger('click')
    await flushPromises()

    expect((wrapper.get('[data-test="submit"]').element as HTMLButtonElement).disabled).toBe(true)
    expect((wrapper.get('[data-test="hint"]').element as HTMLButtonElement).disabled).toBe(true)
    expect((wrapper.get('[data-test="run"]').element as HTMLButtonElement).disabled).toBe(true)
    wrapper.unmount()
  })
})
