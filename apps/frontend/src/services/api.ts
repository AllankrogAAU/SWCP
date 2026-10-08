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

export interface SubmissionNotification {
  submissionId: string
  eventType: string
  stage: string
  message: string
  percentComplete: number
  timestampUtc: string
}

interface ApiProblem {
  title?: string
  detail?: string
  error?: string
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
    throw new Error(message || body || `Request failed (${response.status})`)
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

export function runSource(assignmentId: string, sourceCode: string, llmBackend: LlmBackend) {
  return request<SubmissionAccepted>('/api/submissions/run', {
    method: 'POST',
    body: JSON.stringify({ assignmentId, sourceCode, llmBackend }),
  })
}

export function getSubmission(submissionId: string) {
  return request<SubmissionResponse>(`/api/submissions/${submissionId}`)
}

export function openSubmissionSocket(
  submissionId: string,
  onNotification: (notification: SubmissionNotification) => void,
  onClose: () => void,
): WebSocket {
  const token = getAccessToken()
  const scheme = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const url = new URL(`/ws/submissions/${submissionId}`, `${scheme}//${window.location.host}`)

  const protocols = token ? ['swcp', `bearer.${token}`] : ['swcp']
  const socket = new WebSocket(url, protocols)
  socket.addEventListener('message', event => {
    try {
      onNotification(JSON.parse(String(event.data)) as SubmissionNotification)
    } catch {
      onClose()
    }
  })
  socket.addEventListener('close', onClose)
  socket.addEventListener('error', onClose)
  return socket
}