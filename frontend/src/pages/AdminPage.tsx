/**
 * AdminPage - Admin panel for user management, tenant config, audit logs.
 *
 * Features:
 * - User management table
 * - Tenant configuration
 * - Audit log viewer with filters
 * - System health dashboard
 */

import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  Users,
  Settings,
  FileText,
  Activity,
  CheckCircle2,
  XCircle,
  AlertCircle,
} from 'lucide-react'
import { format } from 'date-fns'
import { Card, CardHeader, CardTitle, CardContent, CardDescription } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Skeleton } from '@/components/ui/skeleton'
import { Separator } from '@/components/ui/separator'
import { api } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import type { User, AuditEntry, UserRole } from '@/types'

export function AdminPage() {
  const { user: currentUser } = useAuth()
  const [selectedTab, setSelectedTab] = useState<'users' | 'config' | 'audit' | 'health'>('users')

  // Only admins can access this page (should be enforced by router)
  if (currentUser?.role !== 'admin') {
    return (
      <div className="flex-1 flex items-center justify-center">
        <div className="text-center">
          <h2 className="text-2xl font-bold mb-2">Access Denied</h2>
          <p className="text-muted-foreground">
            You need admin privileges to access this page
          </p>
        </div>
      </div>
    )
  }

  return (
    <div className="flex-1 p-6 overflow-auto">
      <div className="max-w-7xl mx-auto space-y-6">
        <div>
          <h1 className="text-3xl font-bold mb-2">Administration</h1>
          <p className="text-muted-foreground">
            Manage users, configuration, and system health
          </p>
        </div>

        {/* Tab navigation */}
        <div className="flex gap-2 border-b border-border">
          <TabButton
            active={selectedTab === 'users'}
            onClick={() => setSelectedTab('users')}
            icon={Users}
            label="Users"
          />
          <TabButton
            active={selectedTab === 'config'}
            onClick={() => setSelectedTab('config')}
            icon={Settings}
            label="Configuration"
          />
          <TabButton
            active={selectedTab === 'audit'}
            onClick={() => setSelectedTab('audit')}
            icon={FileText}
            label="Audit Logs"
          />
          <TabButton
            active={selectedTab === 'health'}
            onClick={() => setSelectedTab('health')}
            icon={Activity}
            label="System Health"
          />
        </div>

        {/* Tab content */}
        {selectedTab === 'users' && <UsersTab />}
        {selectedTab === 'config' && <ConfigTab />}
        {selectedTab === 'audit' && <AuditTab />}
        {selectedTab === 'health' && <HealthTab />}
      </div>
    </div>
  )
}

function TabButton({
  active,
  onClick,
  icon: Icon,
  label,
}: {
  active: boolean
  onClick: () => void
  icon: any
  label: string
}) {
  return (
    <button
      onClick={onClick}
      className={`flex items-center gap-2 px-4 py-2 border-b-2 transition-colors ${
        active
          ? 'border-primary text-primary'
          : 'border-transparent text-muted-foreground hover:text-foreground'
      }`}
    >
      <Icon className="w-4 h-4" />
      {label}
    </button>
  )
}

function UsersTab() {
  const { data: users, isLoading } = useQuery({
    queryKey: ['users'],
    queryFn: () => api.getUsers(),
  })

  const queryClient = useQueryClient()

  const updateUserMutation = useMutation({
    mutationFn: ({ id, data }: { id: string; data: Partial<User> }) =>
      api.updateUser(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['users'] })
    },
  })

  const handleToggleActive = (user: User) => {
    updateUserMutation.mutate({
      id: user.id,
      data: { is_active: !user.is_active },
    })
  }

  const handleChangeRole = (user: User, newRole: UserRole) => {
    updateUserMutation.mutate({
      id: user.id,
      data: { role: newRole },
    })
  }

  return (
    <Card>
      <CardHeader>
        <CardTitle>User Management</CardTitle>
        <CardDescription>Manage user roles and access</CardDescription>
      </CardHeader>
      <CardContent>
        {isLoading ? (
          <div className="space-y-3">
            {[...Array(5)].map((_, i) => (
              <Skeleton key={i} className="h-16 w-full" />
            ))}
          </div>
        ) : users && users.length > 0 ? (
          <ScrollArea className="h-[600px]">
            <div className="space-y-3">
              {users.map((user: User) => (
                <div
                  key={user.id}
                  className="flex items-center gap-4 p-4 rounded-md border border-border"
                >
                  <div className="flex-1 min-w-0">
                    <div className="font-medium">{user.email}</div>
                    <div className="text-sm text-muted-foreground">
                      {user.display_name || 'No display name'}
                    </div>
                    {user.last_login_at && (
                      <div className="text-xs text-muted-foreground mt-1">
                        Last login:{' '}
                        {format(new Date(user.last_login_at), 'MMM d, yyyy h:mm a')}
                      </div>
                    )}
                  </div>

                  <select
                    value={user.role}
                    onChange={(e) => handleChangeRole(user, e.target.value as UserRole)}
                    className="px-3 py-1 rounded-md border border-input bg-background text-sm"
                    disabled={updateUserMutation.isPending}
                  >
                    <option value="viewer">Viewer</option>
                    <option value="operator">Operator</option>
                    <option value="admin">Admin</option>
                  </select>

                  <Button
                    variant={user.is_active ? 'default' : 'secondary'}
                    size="sm"
                    onClick={() => handleToggleActive(user)}
                    disabled={updateUserMutation.isPending}
                  >
                    {user.is_active ? 'Active' : 'Inactive'}
                  </Button>
                </div>
              ))}
            </div>
          </ScrollArea>
        ) : (
          <div className="text-center py-12 text-muted-foreground">
            No users found
          </div>
        )}
      </CardContent>
    </Card>
  )
}

function ConfigTab() {
  const { data: config, isLoading } = useQuery({
    queryKey: ['tenant-config'],
    queryFn: () => api.getTenantConfig(),
  })

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader>
          <CardTitle>Tenant Configuration</CardTitle>
          <CardDescription>Configure rate limits and defaults</CardDescription>
        </CardHeader>
        <CardContent>
          {isLoading ? (
            <div className="space-y-4">
              {[...Array(4)].map((_, i) => (
                <Skeleton key={i} className="h-12 w-full" />
              ))}
            </div>
          ) : config ? (
            <div className="space-y-4">
              <div>
                <label className="text-sm font-medium mb-2 block">
                  Tenant Name
                </label>
                <Input value={config.name} disabled />
              </div>

              <div className="grid grid-cols-2 gap-4">
                <div>
                  <label className="text-sm font-medium mb-2 block">
                    Rate Limit (per minute)
                  </label>
                  <Input
                    type="number"
                    value={config.rate_limit_per_minute}
                    disabled
                  />
                </div>
                <div>
                  <label className="text-sm font-medium mb-2 block">
                    Rate Limit (per hour)
                  </label>
                  <Input
                    type="number"
                    value={config.rate_limit_per_hour}
                    disabled
                  />
                </div>
              </div>

              <div>
                <label className="text-sm font-medium mb-2 block">
                  Default Classification
                </label>
                <Input value={config.default_classification} disabled />
              </div>

              <div className="flex items-center gap-2">
                <span className="text-sm font-medium">Status:</span>
                <Badge variant={config.is_active ? 'default' : 'secondary'}>
                  {config.is_active ? 'Active' : 'Inactive'}
                </Badge>
              </div>
            </div>
          ) : null}
        </CardContent>
      </Card>
    </div>
  )
}

function AuditTab() {
  const [filters, setFilters] = useState({
    action: '',
    user_id: '',
  })

  const { data: auditLogs, isLoading } = useQuery({
    queryKey: ['audit-logs', filters],
    queryFn: () => api.getAuditLogs(filters),
  })

  return (
    <Card>
      <CardHeader>
        <CardTitle>Audit Logs</CardTitle>
        <CardDescription>View system activity and security events</CardDescription>
      </CardHeader>
      <CardContent>
        {/* Filters */}
        <div className="flex gap-4 mb-4">
          <select
            value={filters.action}
            onChange={(e) => setFilters({ ...filters, action: e.target.value })}
            className="px-4 py-2 rounded-md border border-input bg-background text-sm"
          >
            <option value="">All Actions</option>
            <option value="chat_send">Chat Send</option>
            <option value="document_upload">Document Upload</option>
            <option value="document_delete">Document Delete</option>
            <option value="user_create">User Create</option>
            <option value="user_update">User Update</option>
            <option value="config_change">Config Change</option>
          </select>
        </div>

        {isLoading ? (
          <div className="space-y-3">
            {[...Array(10)].map((_, i) => (
              <Skeleton key={i} className="h-20 w-full" />
            ))}
          </div>
        ) : auditLogs && auditLogs.length > 0 ? (
          <ScrollArea className="h-[600px]">
            <div className="space-y-2">
              {auditLogs.map((log: AuditEntry) => (
                <div
                  key={log.id}
                  className="p-3 rounded-md border border-border text-sm"
                >
                  <div className="flex items-start justify-between mb-2">
                    <div className="flex items-center gap-2">
                      <Badge variant="outline">{log.action}</Badge>
                      <Badge
                        variant={
                          log.status === 'success'
                            ? 'default'
                            : log.status === 'failure'
                            ? 'destructive'
                            : 'secondary'
                        }
                      >
                        {log.status}
                      </Badge>
                    </div>
                    <span className="text-xs text-muted-foreground">
                      {format(new Date(log.created_at), 'MMM d, yyyy h:mm:ss a')}
                    </span>
                  </div>

                  <div className="space-y-1 text-xs text-muted-foreground">
                    <div>User: {log.user_email}</div>
                    {log.resource_type && (
                      <div>
                        Resource: {log.resource_type}{' '}
                        {log.resource_id && `(${log.resource_id})`}
                      </div>
                    )}
                    <div>IP: {log.ip_address}</div>
                    {log.error_message && (
                      <div className="text-destructive">
                        Error: {log.error_message}
                      </div>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </ScrollArea>
        ) : (
          <div className="text-center py-12 text-muted-foreground">
            No audit logs found
          </div>
        )}
      </CardContent>
    </Card>
  )
}

function HealthTab() {
  const { data: health, isLoading } = useQuery({
    queryKey: ['health'],
    queryFn: () => api.getHealth(),
    refetchInterval: 30000, // Refresh every 30 seconds
  })

  const services = health
    ? [
        { name: 'Database', status: health.database },
        { name: 'Redis', status: health.redis },
        { name: 'LLM Service', status: health.llm_service },
      ]
    : []

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader>
          <CardTitle>System Health</CardTitle>
          <CardDescription>Monitor system components and status</CardDescription>
        </CardHeader>
        <CardContent>
          {isLoading ? (
            <div className="space-y-4">
              {[...Array(4)].map((_, i) => (
                <Skeleton key={i} className="h-16 w-full" />
              ))}
            </div>
          ) : health ? (
            <div className="space-y-6">
              {/* Overall status */}
              <div className="flex items-center gap-4 p-4 rounded-lg bg-accent">
                {health.status === 'healthy' ? (
                  <CheckCircle2 className="w-8 h-8 text-green-500" />
                ) : health.status === 'degraded' ? (
                  <AlertCircle className="w-8 h-8 text-yellow-500" />
                ) : (
                  <XCircle className="w-8 h-8 text-red-500" />
                )}
                <div>
                  <div className="text-lg font-semibold">
                    System Status:{' '}
                    <span
                      className={
                        health.status === 'healthy'
                          ? 'text-green-500'
                          : health.status === 'degraded'
                          ? 'text-yellow-500'
                          : 'text-red-500'
                      }
                    >
                      {health.status.toUpperCase()}
                    </span>
                  </div>
                  <div className="text-sm text-muted-foreground">
                    Version {health.version} • Uptime:{' '}
                    {Math.floor(health.uptime_seconds / 3600)}h{' '}
                    {Math.floor((health.uptime_seconds % 3600) / 60)}m
                  </div>
                </div>
              </div>

              <Separator />

              {/* Services */}
              <div>
                <h3 className="text-lg font-semibold mb-4">Services</h3>
                <div className="grid gap-3">
                  {services.map((service) => (
                    <div
                      key={service.name}
                      className="flex items-center justify-between p-3 rounded-md border border-border"
                    >
                      <span className="font-medium">{service.name}</span>
                      <div className="flex items-center gap-2">
                        {service.status ? (
                          <>
                            <CheckCircle2 className="w-5 h-5 text-green-500" />
                            <span className="text-sm text-green-500">
                              Operational
                            </span>
                          </>
                        ) : (
                          <>
                            <XCircle className="w-5 h-5 text-red-500" />
                            <span className="text-sm text-red-500">Down</span>
                          </>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              </div>
            </div>
          ) : null}
        </CardContent>
      </Card>
    </div>
  )
}
