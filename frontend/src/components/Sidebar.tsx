/**
 * Sidebar component - main navigation for the application.
 *
 * Features:
 * - Navigation links to main pages
 * - Active conversation list
 * - User profile with role badge
 * - New conversation button
 * - Collapsible design
 */

import { useState } from 'react'
import { Link, useLocation } from 'react-router-dom'
import {
  MessageSquare,
  FileText,
  Bot,
  Settings,
  Plus,
  ChevronLeft,
  ChevronRight,
  LogOut,
} from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Avatar, AvatarFallback } from '@/components/ui/avatar'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Separator } from '@/components/ui/separator'
import { Skeleton } from '@/components/ui/skeleton'
import { cn } from '@/lib/utils'
import { useAuth } from '@/lib/auth'
import { api } from '@/lib/api'
import type { Conversation } from '@/types'

export function Sidebar() {
  const [collapsed, setCollapsed] = useState(false)
  const location = useLocation()
  const { user, logout } = useAuth()

  // Fetch conversations
  const { data: conversations, isLoading } = useQuery({
    queryKey: ['conversations'],
    queryFn: () => api.getConversations(),
  })

  const navItems = [
    { href: '/', icon: MessageSquare, label: 'Chat' },
    { href: '/documents', icon: FileText, label: 'Documents' },
    { href: '/agents', icon: Bot, label: 'Agents' },
    ...(user?.role === 'admin'
      ? [{ href: '/admin', icon: Settings, label: 'Admin' }]
      : []),
  ]

  const isActive = (href: string) => {
    if (href === '/') {
      return location.pathname === '/'
    }
    return location.pathname.startsWith(href)
  }

  if (collapsed) {
    return (
      <div className="w-16 bg-card border-r border-border flex flex-col">
        <div className="p-3 flex justify-center">
          <Button
            variant="ghost"
            size="icon"
            onClick={() => setCollapsed(false)}
          >
            <ChevronRight className="w-5 h-5" />
          </Button>
        </div>

        <Separator />

        <nav className="flex-1 p-2 space-y-1">
          {navItems.map((item) => (
            <Link key={item.href} to={item.href}>
              <Button
                variant={isActive(item.href) ? 'secondary' : 'ghost'}
                size="icon"
                className="w-full"
              >
                <item.icon className="w-5 h-5" />
              </Button>
            </Link>
          ))}
        </nav>
      </div>
    )
  }

  return (
    <div className="w-64 bg-card border-r border-border flex flex-col">
      {/* Header */}
      <div className="p-4 flex items-center justify-between">
        <h1 className="text-lg font-semibold">Agent Platform</h1>
        <Button
          variant="ghost"
          size="icon"
          onClick={() => setCollapsed(true)}
        >
          <ChevronLeft className="w-5 h-5" />
        </Button>
      </div>

      <Separator />

      {/* Navigation */}
      <nav className="p-2 space-y-1">
        {navItems.map((item) => (
          <Link key={item.href} to={item.href}>
            <Button
              variant={isActive(item.href) ? 'secondary' : 'ghost'}
              className="w-full justify-start"
            >
              <item.icon className="w-4 h-4 mr-2" />
              {item.label}
            </Button>
          </Link>
        ))}
      </nav>

      <Separator className="my-2" />

      {/* Conversations */}
      <div className="flex-1 overflow-hidden flex flex-col">
        <div className="p-2 flex items-center justify-between">
          <span className="text-sm font-medium text-muted-foreground">
            Conversations
          </span>
          <Link to="/">
            <Button variant="ghost" size="icon" className="h-6 w-6">
              <Plus className="w-4 h-4" />
            </Button>
          </Link>
        </div>

        <ScrollArea className="flex-1 px-2">
          {isLoading ? (
            <div className="space-y-2">
              {[...Array(5)].map((_, i) => (
                <Skeleton key={i} className="h-12 w-full" />
              ))}
            </div>
          ) : conversations && conversations.length > 0 ? (
            <div className="space-y-1">
              {conversations.slice(0, 20).map((conv: Conversation) => (
                <Link key={conv.id} to={`/?conversation=${conv.id}`}>
                  <div
                    className={cn(
                      'p-2 rounded-md hover:bg-accent cursor-pointer transition-colors',
                      location.search.includes(conv.id) && 'bg-accent'
                    )}
                  >
                    <div className="text-sm font-medium truncate">
                      {conv.title}
                    </div>
                    <div className="text-xs text-muted-foreground">
                      {conv.message_count} messages
                    </div>
                  </div>
                </Link>
              ))}
            </div>
          ) : (
            <div className="text-sm text-muted-foreground text-center py-4">
              No conversations yet
            </div>
          )}
        </ScrollArea>
      </div>

      <Separator className="my-2" />

      {/* User profile */}
      <div className="p-3">
        <div className="flex items-center gap-3 p-2 rounded-md bg-accent/50">
          <Avatar>
            <AvatarFallback>
              {user?.display_name?.charAt(0) || user?.email.charAt(0) || 'U'}
            </AvatarFallback>
          </Avatar>
          <div className="flex-1 min-w-0">
            <div className="text-sm font-medium truncate">
              {user?.display_name || user?.email || 'User'}
            </div>
            <Badge variant="secondary" className="mt-1 text-xs">
              {user?.role}
            </Badge>
          </div>
          <Button variant="ghost" size="icon" onClick={logout}>
            <LogOut className="w-4 h-4" />
          </Button>
        </div>
      </div>
    </div>
  )
}
