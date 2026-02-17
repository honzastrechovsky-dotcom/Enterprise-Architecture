import { getAuthToken } from '@/lib/auth'
import type {
  User,
  Conversation,
  ChatRequest,
  Document,
  DocumentClassification,
  Agent,
  Skill,
  AuditEntry,
  TenantConfig,
  SystemHealth,
  SSEEvent,
} from '@/types'

const API_BASE_URL = import.meta.env.VITE_API_URL || ''

/**
 * Authenticated fetch wrapper. Adds JWT from localStorage and handles errors.
 */
async function apiFetch<T>(path: string, options: RequestInit = {}): Promise<T> {
  const token = getAuthToken()
  const headers: Record<string, string> = {
    ...(options.headers as Record<string, string> || {}),
  }

  if (token) {
    headers['Authorization'] = `Bearer ${token}`
  }

  // Only set Content-Type for non-FormData bodies
  if (options.body && !(options.body instanceof FormData)) {
    headers['Content-Type'] = 'application/json'
  }

  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...options,
    headers,
  })

  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: response.statusText }))
    throw new Error(error.detail || `API error: ${response.status}`)
  }

  return response.json() as Promise<T>
}

/**
 * Stream chat via SSE. Returns an async iterable of SSEEvent.
 */
async function* streamChat(request: ChatRequest): AsyncGenerator<SSEEvent> {
  const token = getAuthToken()
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
  }

  if (token) {
    headers['Authorization'] = `Bearer ${token}`
  }

  const response = await fetch(`${API_BASE_URL}/api/v1/chat/stream`, {
    method: 'POST',
    headers,
    body: JSON.stringify(request),
  })

  if (!response.ok) {
    const error = await response.json().catch(() => ({ detail: response.statusText }))
    throw new Error(error.detail || `Stream error: ${response.status}`)
  }

  const reader = response.body?.getReader()
  if (!reader) {
    throw new Error('No response body for streaming')
  }

  const decoder = new TextDecoder()
  let buffer = ''

  try {
    while (true) {
      const { done, value } = await reader.read()
      if (done) break

      buffer += decoder.decode(value, { stream: true })
      const lines = buffer.split('\n')
      buffer = lines.pop() || ''

      for (const line of lines) {
        if (line.startsWith('data: ')) {
          const data = line.slice(6).trim()
          if (data === '[DONE]') return

          try {
            const event = JSON.parse(data) as SSEEvent
            yield event
          } catch {
            // If not JSON, treat as token
            yield { type: 'token', data }
          }
        } else if (line.startsWith('event: ')) {
          // SSE event type line - handled with subsequent data line
          continue
        }
      }
    }
  } finally {
    reader.releaseLock()
  }
}

/**
 * API client with typed methods for each endpoint.
 */
export const api = {
  // Conversations
  getConversations: () =>
    apiFetch<Conversation[]>('/api/v1/conversations'),

  getConversation: (id: string) =>
    apiFetch<Conversation>(`/api/v1/conversations/${id}`),

  // Chat
  streamChat,

  // Documents
  getDocuments: () =>
    apiFetch<Document[]>('/api/v1/documents'),

  deleteDocument: (id: string) =>
    apiFetch<void>(`/api/v1/documents/${id}`, { method: 'DELETE' }),

  uploadDocument: (file: File, classification: DocumentClassification) => {
    const formData = new FormData()
    formData.append('file', file)
    formData.append('classification', classification)
    return apiFetch<{ document_id: string; filename: string; status: string }>(
      '/api/v1/documents/upload',
      { method: 'POST', body: formData }
    )
  },

  // Agents & Skills
  getAgents: () =>
    apiFetch<Agent[]>('/api/v1/agents'),

  getSkills: () =>
    apiFetch<Skill[]>('/api/v1/skills'),

  // Admin - Users
  getUsers: () =>
    apiFetch<User[]>('/api/v1/admin/users'),

  updateUser: (id: string, data: Partial<User>) =>
    apiFetch<User>(`/api/v1/admin/users/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(data),
    }),

  // Admin - Config
  getTenantConfig: () =>
    apiFetch<TenantConfig>('/api/v1/admin/config'),

  // Admin - Audit
  getAuditLogs: (filters: { action?: string; user_id?: string }) => {
    const params = new URLSearchParams()
    if (filters.action) params.set('action', filters.action)
    if (filters.user_id) params.set('user_id', filters.user_id)
    const query = params.toString()
    return apiFetch<AuditEntry[]>(`/api/v1/admin/audit${query ? `?${query}` : ''}`)
  },

  // Admin - Health
  getHealth: () =>
    apiFetch<SystemHealth>('/api/v1/health'),
}
