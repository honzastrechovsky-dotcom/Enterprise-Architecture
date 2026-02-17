/**
 * AgentsPage - Agent overview and management.
 *
 * Features:
 * - Grid of agent cards
 * - Agent capabilities display
 * - Status indicators
 * - Role-based access display
 */

import { useQuery } from '@tanstack/react-query'
import { Bot, CheckCircle2, Circle, Shield } from 'lucide-react'
import { Card, CardHeader, CardTitle, CardDescription, CardContent } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { Skeleton } from '@/components/ui/skeleton'
import { api } from '@/lib/api'
import { useAuth } from '@/lib/auth'
import type { Agent, Skill } from '@/types'

const AGENT_TYPE_LABELS: Record<string, string> = {
  generalist: 'Generalist',
  document_analyst: 'Document Analyst',
  data_analyst: 'Data Analyst',
  procedure_expert: 'Procedure Expert',
  quality_inspector: 'Quality Inspector',
  maintenance_advisor: 'Maintenance Advisor',
}

export function AgentsPage() {
  useAuth()

  const { data: agents, isLoading } = useQuery({
    queryKey: ['agents'],
    queryFn: () => api.getAgents(),
  })

  const { data: skills } = useQuery({
    queryKey: ['skills'],
    queryFn: () => api.getSkills(),
  })

  return (
    <div className="flex-1 p-6 overflow-auto">
      <div className="max-w-7xl mx-auto space-y-6">
        <div>
          <h1 className="text-3xl font-bold mb-2">Agents</h1>
          <p className="text-muted-foreground">
            Specialized AI agents available in your organization
          </p>
        </div>

        {/* Agent grid */}
        {isLoading ? (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
            {[...Array(6)].map((_, i) => (
              <Skeleton key={i} className="h-64" />
            ))}
          </div>
        ) : agents && agents.length > 0 ? (
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-6">
            {agents.map((agent: Agent) => (
              <Card
                key={agent.id}
                className="hover:shadow-lg transition-shadow"
              >
                <CardHeader>
                  <div className="flex items-start justify-between mb-2">
                    <div className="w-12 h-12 rounded-lg bg-primary/10 flex items-center justify-center">
                      <Bot className="w-6 h-6 text-primary" />
                    </div>
                    {agent.is_active ? (
                      <Badge variant="default" className="gap-1">
                        <CheckCircle2 className="w-3 h-3" />
                        Active
                      </Badge>
                    ) : (
                      <Badge variant="secondary" className="gap-1">
                        <Circle className="w-3 h-3" />
                        Inactive
                      </Badge>
                    )}
                  </div>

                  <CardTitle>{agent.name}</CardTitle>
                  <CardDescription>
                    {AGENT_TYPE_LABELS[agent.type] || agent.type}
                  </CardDescription>
                </CardHeader>

                <CardContent className="space-y-4">
                  <p className="text-sm text-muted-foreground">
                    {agent.description}
                  </p>

                  {/* Capabilities */}
                  <div>
                    <div className="text-sm font-medium mb-2">
                      Capabilities:
                    </div>
                    <div className="space-y-2">
                      {agent.capabilities.slice(0, 3).map((cap, idx) => (
                        <div
                          key={idx}
                          className="text-sm text-muted-foreground flex items-start gap-2"
                        >
                          <div className="w-1 h-1 rounded-full bg-primary mt-2 flex-shrink-0" />
                          <span>{cap.description}</span>
                        </div>
                      ))}
                      {agent.capabilities.length > 3 && (
                        <div className="text-xs text-muted-foreground">
                          +{agent.capabilities.length - 3} more
                        </div>
                      )}
                    </div>
                  </div>

                  {/* Required role */}
                  {agent.capabilities.length > 0 && (
                    <div className="flex items-center gap-2 text-sm">
                      <Shield className="w-4 h-4 text-muted-foreground" />
                      <span className="text-muted-foreground">
                        Requires: {agent.capabilities[0].required_role}
                      </span>
                    </div>
                  )}

                  {/* Last used */}
                  {agent.last_used_at && (
                    <div className="text-xs text-muted-foreground">
                      Last used:{' '}
                      {new Date(agent.last_used_at).toLocaleDateString()}
                    </div>
                  )}
                </CardContent>
              </Card>
            ))}
          </div>
        ) : (
          <div className="text-center py-12 text-muted-foreground">
            No agents available
          </div>
        )}

        {/* Skills section */}
        {skills && skills.length > 0 && (
          <div className="mt-12">
            <h2 className="text-2xl font-bold mb-4">Available Skills</h2>
            <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
              {skills.map((skill: Skill) => (
                <Card key={skill.name}>
                  <CardHeader>
                    <div className="flex items-center justify-between">
                      <CardTitle className="text-base">{skill.name}</CardTitle>
                      {skill.is_builtin && (
                        <Badge variant="secondary">Built-in</Badge>
                      )}
                    </div>
                    <CardDescription>{skill.category}</CardDescription>
                  </CardHeader>
                  <CardContent>
                    <p className="text-sm text-muted-foreground mb-3">
                      {skill.description}
                    </p>
                    {skill.required_permissions.length > 0 && (
                      <div className="text-xs text-muted-foreground">
                        Permissions: {skill.required_permissions.join(', ')}
                      </div>
                    )}
                  </CardContent>
                </Card>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
