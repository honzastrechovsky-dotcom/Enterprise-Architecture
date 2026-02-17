/**
 * TypeScript type definitions for the Enterprise Agent Platform frontend.
 * These types mirror the backend Pydantic models and API responses.
 */

// ============================================================================
// User & Authentication
// ============================================================================

export type UserRole = 'admin' | 'operator' | 'viewer'

export interface User {
  id: string
  email: string
  display_name: string | null
  role: UserRole
  tenant_id: string
  is_active: boolean
  last_login_at: string | null
  created_at: string
}

export interface AuthToken {
  access_token: string
  token_type: string
}

// ============================================================================
// Conversations & Messages
// ============================================================================

export interface Message {
  id: string
  conversation_id: string
  role: 'user' | 'agent'
  content: string
  agent_name?: string
  citations: Citation[]
  verification_status?: 'verified' | 'unverified' | 'flagged'
  created_at: string
}

export interface Citation {
  index: number
  document_id: string
  document_name: string
  document_version: string
  chunk_index: number
  content_snippet: string
  page_number: number | null
  section: string | null
}

export interface Conversation {
  id: string
  user_id: string
  title: string
  created_at: string
  updated_at: string
  message_count: number
}

export interface ChatRequest {
  message: string
  conversation_id?: string
  model_override?: string
}

export interface ChatResponse {
  response: string
  conversation_id: string
  citations: Citation[]
  model_used: string
  latency_ms: number
}

// ============================================================================
// Documents
// ============================================================================

export type DocumentClassification = 'CLASS_I' | 'CLASS_II' | 'CLASS_III' | 'CLASS_IV'
export type DocumentStatus = 'uploading' | 'processing' | 'ready' | 'error'

export interface Document {
  id: string
  tenant_id: string
  filename: string
  file_type: string
  file_size_bytes: number
  classification: DocumentClassification
  status: DocumentStatus
  error_message: string | null
  chunk_count: number
  version: string
  uploaded_by: string
  uploaded_at: string
  processed_at: string | null
}

export interface DocumentUploadResponse {
  document_id: string
  filename: string
  status: DocumentStatus
}

// ============================================================================
// Agents & Skills
// ============================================================================

export interface AgentCapability {
  name: string
  description: string
  required_role: UserRole
}

export interface Agent {
  id: string
  name: string
  type: 'generalist' | 'document_analyst' | 'data_analyst' | 'procedure_expert' | 'quality_inspector' | 'maintenance_advisor'
  description: string
  capabilities: AgentCapability[]
  is_active: boolean
  last_used_at: string | null
}

export interface Skill {
  name: string
  description: string
  category: string
  required_permissions: string[]
  is_builtin: boolean
}

// ============================================================================
// Admin & Audit
// ============================================================================

export type AuditAction = 'chat_send' | 'document_upload' | 'document_delete' | 'user_create' | 'user_update' | 'config_change'
export type AuditStatus = 'success' | 'failure' | 'rate_limited'

export interface AuditEntry {
  id: string
  tenant_id: string
  user_id: string
  user_email: string
  action: AuditAction
  resource_type: string | null
  resource_id: string | null
  status: AuditStatus
  error_message: string | null
  request_id: string
  ip_address: string
  user_agent: string
  created_at: string
}

export interface TenantConfig {
  id: string
  name: string
  rate_limit_per_minute: number
  rate_limit_per_hour: number
  default_classification: DocumentClassification
  is_active: boolean
}

export interface SystemHealth {
  status: 'healthy' | 'degraded' | 'down'
  database: boolean
  redis: boolean
  llm_service: boolean
  uptime_seconds: number
  version: string
}

// ============================================================================
// SSE (Server-Sent Events) Streaming
// ============================================================================

export type SSEEventType = 'token' | 'citation' | 'complete' | 'error'

export interface SSEEvent {
  type: SSEEventType
  data: string | Citation | { error: string }
}

// ============================================================================
// API Response Wrappers
// ============================================================================

export interface ApiError {
  detail: string
}

export interface PaginatedResponse<T> {
  items: T[]
  total: number
  page: number
  page_size: number
}
