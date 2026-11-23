import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { flushPromises } from '@vue/test-utils'
import App from '../App.vue'

const api = vi.hoisted(() => ({
  ApiError: class ApiError extends Error {
    status: number
    activeSubmissionId?: string
    submissionStatus?: string

    constructor(
      message: string,
      status: number,
      activeSubmissionId?: string,
      submissionStatus?: string,
    ) {
      super(message)
      this.name = 'ApiError'
      this.status = status
      this.activeSubmissionId = activeSubmissionId
      this.submissionStatus = submissionStatus
    }
  },
  cancelSubmission: vi.fn<(...args: unknown[]) => unknown>(),
  clearAccessToken: vi.fn<(...args: unknown[]) => unknown>(),
  getAccessToken: vi.fn<(...args: unknown[]) => unknown>(),
  getAssignments: vi.fn<(...args: unknown[]) => unknown>(),
  getAssignmentState: vi.fn<(...args: unknown[]) => unknown>(),
  saveAssignmentState: vi.fn<(...args: unknown[]) => unknown>(),
  getSubmissionHistory: vi.fn<(...args: unknown[]) => unknown>(),
  getSubmission: vi.fn<(...args: unknown[]) => unknown>(),
  login: vi.fn<(...args: unknown[]) => unknown>(),
  streamSubmissionEvents: vi.fn<(...args: unknown[]) => unknown>(),
  register: vi.fn<(...args: unknown[]) => unknown>(),
  setAccessToken: vi.fn<(...args: unknown[]) => unknown>(),
  submitSource: vi.fn<(...args: unknown[]) => unknown>(),
  requestHint: vi.fn<(...args: unknown[]) => unknown>(),
  runSource: vi.fn<(...args: unknown[]) => unknown>(),
}))

vi.mock('../services/api', () => api)

vi.mock('../Components/CodeEditorBox.vue', () => ({
  default: {
    props: ['busyAction', 'terminalOutput', 'modelValue', 'standardInput'],
    emits: ['submit', 'hint', 'run', 'update:standardInput'],
    template:
      '<div><textarea data-test="code" :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" /><textarea data-test="stdin" :value="standardInput" @input="$emit(\'update:standardInput\', $event.target.value)" /><button data-test="submit" :disabled="busyAction !== null" @click="$emit(\'submit\')">Submit</button><button data-test="hint" :disabled="busyAction !== null" @click="$emit(\'hint\')">Hint</button><button data-test="run" :disabled="busyAction !== null" @click="$emit(\'run\')">Run</button><pre>{{ terminalOutput }}</pre></div>',
  },
}))

vi.mock('../Components/AssignmentList.vue', () => ({
  default: {
    props: ['assignments', 'selectedId'],
    emits: ['select'],
    template:
      '<div><button v-for="assignment in assignments" :key="assignment.id" :data-test="`assignment-${assignment.id}`" @click="$emit(\'select\', assignment.id)">{{ assignment.title }}</button></div>',
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
    api.getAssignmentState.mockResolvedValue({
      currentCode: 'initial code',
      updatedAtUtc: '2026-11-10T00:00:00Z',
    })
    api.saveAssignmentState.mockResolvedValue(undefined)
    api.getSubmissionHistory.mockResolvedValue([])
    api.submitSource.mockResolvedValue({ submissionId: 'submission-id', status: 'SANDBOX_QUEUED' })
    api.requestHint.mockResolvedValue({ submissionId: 'hint-id', status: 'SANDBOX_QUEUED' })
    api.runSource.mockResolvedValue({ submissionId: 'run-id', status: 'SANDBOX_QUEUED' })
    api.cancelSubmission.mockResolvedValue({
      submissionId: 'submission-id',
      assignmentId: 'assignment-id',
      action: 'submit',
      status: 'FAILED',
      errorMessage: 'Canceled by user.',
      sandboxOutput: null,
      taskSolved: false,
      llmFeedback: null,
    })
    api.getSubmission.mockResolvedValue({
      submissionId: 'submission-id',
      assignmentId: 'assignment-id',
      action: 'submit',
      status: 'COMPLETED',
      llmFeedback: 'Looks good.',
      sandboxOutput: null,
      taskSolved: true,
      errorMessage: null,
    })
    api.streamSubmissionEvents.mockReturnValue(vi.fn())
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
    expect(wrapper.text()).toContain('Hello C')

    await wrapper.get('[data-test="submit"]').trigger('click')
    await flushPromises()

    expect(api.submitSource).toHaveBeenCalledWith('assignment-id', expect.any(String), 'azure')
    expect(api.streamSubmissionEvents).toHaveBeenCalledWith(
      'submission-id',
      expect.any(Function),
      expect.any(Function),
    )
    expect(api.getSubmission).toHaveBeenCalledWith('submission-id')
    expect(wrapper.text()).toContain('Looks good.')
    expect(wrapper.html()).toContain('Task judged correctly solved')
  })

  it('closes the submission event stream when unmounted', async () => {
    const closeStream = vi.fn<() => void>()
    let resolveSubmission: ((submission: unknown) => void) | undefined
    api.streamSubmissionEvents.mockReturnValue(closeStream)
    api.getSubmission.mockReturnValue(
      new Promise((resolve) => {
        resolveSubmission = resolve
      }),
    )

    const wrapper = mount(App)
    await wrapper.find('v-form').trigger('submit')
    await flushPromises()
    await wrapper.get('[data-test="submit"]').trigger('click')
    await flushPromises()

    wrapper.unmount()

    expect(closeStream).toHaveBeenCalledOnce()
    resolveSubmission?.({ status: 'SANDBOX_QUEUED' })
    await flushPromises()
  })

  it('tracks Hint results as feedback and Run results as terminal output only', async () => {
    const wrapper = mount(App)
    await wrapper.find('v-form').trigger('submit')
    await flushPromises()

    api.getSubmission.mockResolvedValueOnce({
      submissionId: 'hint-id',
      assignmentId: 'assignment-id',
      action: 'hint',
      status: 'COMPLETED',
      llmFeedback: 'Try checking the loop condition.',
      sandboxOutput: null,
      taskSolved: null,
      errorMessage: null,
    })
    await wrapper.get('[data-test="hint"]').trigger('click')
    await flushPromises()
    expect(api.requestHint).toHaveBeenCalledWith('assignment-id', expect.any(String), 'azure')
    expect(api.getSubmission).toHaveBeenCalledWith('hint-id')
    expect(wrapper.text()).toContain('Try checking the loop condition.')

    api.getSubmission.mockResolvedValueOnce({
      submissionId: 'run-id',
      assignmentId: 'assignment-id',
      action: 'run',
      status: 'COMPLETED',
      llmFeedback: 'This must not be displayed.',
      sandboxOutput: { compilation: { stdout: 'program output', stderr: '' }, testResults: [] },
      taskSolved: null,
      errorMessage: null,
    })
    await wrapper.get('[data-test="stdin"]').setValue('2 3\n')
    await wrapper.get('[data-test="run"]').trigger('click')
    await flushPromises()
    expect(api.runSource).toHaveBeenCalledWith(
      'assignment-id',
      expect.any(String),
      'azure',
      '2 3\n',
    )
    expect(api.getSubmission).toHaveBeenCalledWith('run-id')
    expect(wrapper.text()).toContain('program output')
    expect(wrapper.text()).not.toContain('This must not be displayed.')
    expect(wrapper.text()).not.toContain('Try checking the loop condition.')
  })

  it('loads the latest Submit verdict from submission history', async () => {
    api.getSubmissionHistory.mockResolvedValue([
      {
        submissionId: 'prior-submit',
        assignmentId: 'assignment-id',
        action: 'submit',
        status: 'COMPLETED',
        taskSolved: false,
        createdAtUtc: '2026-10-08T20:00:00Z',
      },
    ])
    const wrapper = mount(App)
    await wrapper.find('v-form').trigger('submit')
    await flushPromises()

    expect(wrapper.html()).toContain('Task judged not solved')
  })

  it('cancels a pending autosave when switching assignments', async () => {
    vi.useFakeTimers()
    let resolveSecondState!: (value: { currentCode: string; updatedAtUtc: string }) => void
    api.getAssignments.mockResolvedValue([
      { id: 'first', title: 'First', description: 'First assignment.' },
      { id: 'second', title: 'Second', description: 'Second assignment.' },
    ])
    api.getAssignmentState.mockImplementation((id) => {
      if (id === 'first') {
        return Promise.resolve({
          currentCode: 'first assignment code',
          updatedAtUtc: '2026-11-10T00:00:00Z',
        })
      }
      return new Promise((resolve) => {
        resolveSecondState = resolve
      })
    })

    const wrapper = mount(App)
    await wrapper.find('v-form').trigger('submit')
    await flushPromises()
    await wrapper.get('[data-test="code"]').setValue('unsaved first assignment code')
    await wrapper.get('[data-test="assignment-second"]').trigger('click')

    await vi.advanceTimersByTimeAsync(1000)
    expect(api.saveAssignmentState).not.toHaveBeenCalled()

    resolveSecondState({
      currentCode: 'second assignment code',
      updatedAtUtc: '2026-11-10T00:00:00Z',
    })
    await flushPromises()
    await wrapper.get('[data-test="code"]').setValue('edited second assignment code')
    await vi.advanceTimersByTimeAsync(1000)

    expect(api.saveAssignmentState).toHaveBeenCalledExactlyOnceWith(
      'second',
      'edited second assignment code',
    )
    wrapper.unmount()
    vi.useRealTimers()
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

  it('displays authentication error when login fails', async () => {
    api.login.mockRejectedValueOnce(new Error('Invalid username or password.'))
    const wrapper = mount(App)
    await wrapper.find('v-form').trigger('submit')
    await flushPromises()

    expect(wrapper.text()).toContain('Invalid username or password.')
    expect(wrapper.text()).toContain('Sign in')
    wrapper.unmount()
  })

  it('handles submission error gracefully and resets busy state', async () => {
    const wrapper = mount(App)
    await wrapper.find('v-form').trigger('submit')
    await flushPromises()

    api.submitSource.mockRejectedValueOnce(new Error('Network error during submit.'))
    await wrapper.get('[data-test="submit"]').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('Network error during submit.')
    expect((wrapper.get('[data-test="submit"]').element as HTMLButtonElement).disabled).toBe(false)
    wrapper.unmount()
  })

  it('displays error and completes when submission polling returns FAILED status', async () => {
    const wrapper = mount(App)
    await wrapper.find('v-form').trigger('submit')
    await flushPromises()

    api.getSubmission.mockResolvedValueOnce({
      submissionId: 'failed-id',
      assignmentId: 'assignment-id',
      action: 'submit',
      status: 'FAILED',
      errorMessage: 'Execution timed out in sandbox.',
      sandboxOutput: null,
      taskSolved: false,
      llmFeedback: null,
    })

    await wrapper.get('[data-test="submit"]').trigger('click')
    await flushPromises()

    expect(wrapper.text()).toContain('Execution timed out in sandbox.')
    expect((wrapper.get('[data-test="submit"]').element as HTMLButtonElement).disabled).toBe(false)
    wrapper.unmount()
  })

  it('resumes tracking and allows canceling an active submission on 409 conflict', async () => {
    const wrapper = mount(App)
    await wrapper.find('v-form').trigger('submit')
    await flushPromises()

    api.requestHint.mockRejectedValueOnce(
      new api.ApiError(
        'Wait for your current code action to finish before starting another.',
        409,
        'stuck-hint-id',
        'LLM_QUEUED',
      ),
    )
    api.getSubmission.mockResolvedValueOnce({
      submissionId: 'stuck-hint-id',
      assignmentId: 'assignment-id',
      action: 'hint',
      status: 'LLM_QUEUED',
      errorMessage: null,
      sandboxOutput: null,
      taskSolved: null,
      llmFeedback: null,
    })

    await wrapper.get('[data-test="hint"]').trigger('click')
    await flushPromises()

    expect(api.getSubmission).toHaveBeenCalledWith('stuck-hint-id')
    expect((wrapper.get('[data-test="hint"]').element as HTMLButtonElement).disabled).toBe(true)

    const cancelButtons = wrapper
      .findAll('button, v-btn')
      .filter((btn) => btn.text().includes('Cancel'))
    expect(cancelButtons.length).toBeGreaterThan(0)
    await cancelButtons[0]!.trigger('click')
    await flushPromises()

    expect(api.cancelSubmission).toHaveBeenCalledWith('stuck-hint-id')
    expect(wrapper.text()).toContain('Canceled by user.')
    expect((wrapper.get('[data-test="hint"]').element as HTMLButtonElement).disabled).toBe(false)
    wrapper.unmount()
  })
})
