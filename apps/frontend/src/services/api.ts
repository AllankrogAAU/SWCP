export type LlmBackend = 'azure' | 'local'
export type SubmissionAction = 'run' | 'hint' | 'submit'
export type SubmissionStatus =
  | 'PENDING'
  | 'SANDBOX_QUEUED'
  | 'SANDBOX_PROCESSING'
  | 'LLM_QUEUED'
  | 'LLM_PROCESSING'
  | 'COMPLETED'
  | 'FAILED'

export interface Assignment {
  id: string
  title: string
  description: string
}

export interface AssignmentState {
  currentCode: string
  updatedAtUtc: string
}

export interface LoginResponse {
  accessToken: string
  tokenType: string
  expiresAtUtc: string
}

export interface SubmissionAccepted {
  submissionId: string
  status: SubmissionStatus
}

export interface SubmissionListItem {
  submissionId: string
  assignmentId: string
  action: SubmissionAction
  status: SubmissionStatus
  taskSolved: boolean | null
  createdAtUtc: string
}

export interface SubmissionResponse {
  submissionId: string
  assignmentId: string
  llmBackend: LlmBackend
  action: SubmissionAction
  status: SubmissionStatus
  retryCount: number
  sandboxOutput: unknown | null
  llmFeedback: string | null
  taskSolved: boolean | null
  errorMessage: string | null
  createdAtUtc: string
  updatedAtUtc: string
}

interface ApiProblem {
  title?: string
  detail?: string
  error?: string
  activeSubmissionId?: string
  status?: SubmissionStatus
}

export class ApiError extends Error {
  readonly status: number
  readonly activeSubmissionId?: string
  readonly submissionStatus?: SubmissionStatus

  constructor(
    message: string,
    status: number,
    activeSubmissionId?: string,
    submissionStatus?: SubmissionStatus,
  ) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.activeSubmissionId = activeSubmissionId
    this.submissionStatus = submissionStatus
  }
}

const tokenKey = 'swcp.accessToken'

export function getAccessToken(): string | null {
  return localStorage.getItem(tokenKey)
}

export function setAccessToken(token: string): void {
  localStorage.setItem(tokenKey, token)
}

export function clearAccessToken(): void {
  localStorage.removeItem(tokenKey)
}

async function request<T>(path: string, init: RequestInit = {}, authenticated = true): Promise<T> {
  const headers = new Headers(init.headers)
  if (init.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')

  if (authenticated) {
    const token = getAccessToken()
    if (token) headers.set('Authorization', `Bearer ${token}`)
  }

  const response = await fetch(path, { ...init, headers })
  if (!response.ok) {
    const body = await response.text()
    let problem: ApiProblem | undefined
    try {
      problem = JSON.parse(body) as ApiProblem
    } catch {
      // Preserve non-JSON error bodies as-is.
    }
    const message = [problem?.title ?? problem?.error, problem?.detail].filter(Boolean).join(': ')
    throw new ApiError(
      message || body || `Request failed (${response.status})`,
      response.status,
      problem?.activeSubmissionId,
      problem?.status,
    )
  }

  if (response.status === 204) return undefined as T
  return (await response.json()) as T
}

export function register(username: string, password: string) {
  return request<{ id: string; username: string; role: string }>(
    '/api/auth/register',
    { method: 'POST', body: JSON.stringify({ username, password }) },
    false,
  )
}

export function login(username: string, password: string) {
  return request<LoginResponse>(
    '/api/auth/login',
    { method: 'POST', body: JSON.stringify({ username, password }) },
    false,
  )
}

export function getAssignments() {
  return request<Assignment[]>('/api/assignments/')
}

export function getAssignmentState(assignmentId: string) {
  return request<AssignmentState>(`/api/assignments/${assignmentId}/state`)
}

export function saveAssignmentState(assignmentId: string, currentCode: string) {
  return request<void>(`/api/assignments/${assignmentId}/state`, {
    method: 'PUT',
    body: JSON.stringify({
      currentCode,
    }),
  })
}

export function getSubmissionHistory() {
  return request<SubmissionListItem[]>('/api/submissions/')
}

export function submitSource(assignmentId: string, sourceCode: string, llmBackend: LlmBackend) {
  return request<SubmissionAccepted>('/api/submissions/', {
    method: 'POST',
    body: JSON.stringify({ assignmentId, sourceCode, llmBackend }),
  })
}

export function requestHint(assignmentId: string, sourceCode: string, llmBackend: LlmBackend) {
  return request<SubmissionAccepted>('/api/submissions/hint', {
    method: 'POST',
    body: JSON.stringify({ assignmentId, sourceCode, llmBackend }),
  })
}

export function runSource(
  assignmentId: string,
  sourceCode: string,
  llmBackend: LlmBackend,
  standardInput: string,
) {
  return request<SubmissionAccepted>('/api/submissions/run', {
    method: 'POST',
    body: JSON.stringify({ assignmentId, sourceCode, llmBackend, standardInput }),
  })
}

export function getSubmission(submissionId: string) {
  return request<SubmissionResponse>(`/api/submissions/${submissionId}`)
}

export function cancelSubmission(submissionId: string) {
  return request<SubmissionResponse>(`/api/submissions/${submissionId}/cancel`, {
    method: 'POST',
  })
}

export function streamSubmissionEvents(
  submissionId: string,
  onCompleted: (result: SubmissionResponse) => void,
  onError: () => void,
): () => void {
  const token = getAccessToken()
  const controller = new AbortController()

  const headers: Record<string, string> = {
    Accept: 'text/event-stream',
  }
  if (token) {
    headers['Authorization'] = `Bearer ${token}`
  }

  fetch(`/api/submissions/${submissionId}/events`, {
    headers,
    signal: controller.signal,
  })
    .then(async (response) => {
      if (!response.ok) {
        throw new Error(`HTTP ${response.status}`)
      }
      const reader = response.body?.getReader()
      if (!reader) throw new Error('No body')
      const decoder = new TextDecoder()
      let buffer = ''

      while (true) {
        const { done, value } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const parts = buffer.split('\n\n')
        buffer = parts.pop() ?? ''

        for (const block of parts) {
          if (!block.trim() || block.startsWith(':')) continue
          let eventType = 'message'
          let dataStr = ''
          for (const line of block.split('\n')) {
            if (line.startsWith('event: ')) {
              eventType = line.slice(7).trim()
            } else if (line.startsWith('data: ')) {
              dataStr += line.slice(6)
            }
          }
          if (dataStr && eventType === 'completed') {
            try {
              const parsed = JSON.parse(dataStr)
              onCompleted(parsed as SubmissionResponse)
            } catch {
              // Ignore invalid JSON
            }
          }
        }
      }
    })
    .catch(() => {
      if (!controller.signal.aborted) {
        onError()
      }
    })

  return () => controller.abort()
}
