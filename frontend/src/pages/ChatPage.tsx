/**
 * ChatPage - Main conversation interface.
 *
 * Features:
 * - Message list with auto-scroll
 * - Input bar with send button
 * - SSE streaming support (token-by-token)
 * - Citation display
 * - AI disclosure on agent messages
 * - Conversation history sidebar
 */

import { useState, useEffect, useRef } from 'react'
import { useSearchParams } from 'react-router-dom'
import { Send, Loader2 } from 'lucide-react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { ScrollArea } from '@/components/ui/scroll-area'
import { ChatMessage } from '@/components/ChatMessage'
import { Skeleton } from '@/components/ui/skeleton'
import { api } from '@/lib/api'
import type { Message, ChatRequest } from '@/types'

export function ChatPage() {
  const [searchParams] = useSearchParams()
  const conversationId = searchParams.get('conversation') || undefined
  const [messages, setMessages] = useState<Message[]>([])
  const [input, setInput] = useState('')
  const [isStreaming, setIsStreaming] = useState(false)
  const [streamingContent, setStreamingContent] = useState('')
  const scrollRef = useRef<HTMLDivElement>(null)
  const queryClient = useQueryClient()

  // Fetch existing conversation messages
  const { data: conversation, isLoading } = useQuery({
    queryKey: ['conversation', conversationId],
    queryFn: () =>
      conversationId ? api.getConversation(conversationId) : null,
    enabled: !!conversationId,
  })

  // Send message mutation
  const sendMutation = useMutation({
    mutationFn: async (request: ChatRequest) => {
      // Use streaming
      setIsStreaming(true)
      setStreamingContent('')

      const citations: any[] = []
      let fullResponse = ''
      let newConversationId = request.conversation_id

      try {
        for await (const event of api.streamChat(request)) {
          if (event.type === 'token') {
            fullResponse += event.data as string
            setStreamingContent(fullResponse)
          } else if (event.type === 'citation') {
            citations.push(event.data)
          } else if (event.type === 'complete') {
            // Parse completion data
            const completeData =
              typeof event.data === 'string' ? JSON.parse(event.data) : event.data
            newConversationId = completeData.conversation_id
          } else if (event.type === 'error') {
            throw new Error(
              typeof event.data === 'object' && 'error' in event.data
                ? event.data.error
                : 'Streaming error'
            )
          }
        }
      } finally {
        setIsStreaming(false)
      }

      return {
        response: fullResponse,
        conversation_id: newConversationId!,
        citations,
        model_used: 'claude-3-5-sonnet',
        latency_ms: 0,
      }
    },
    onSuccess: (data) => {
      // Add agent message to local state
      const agentMessage: Message = {
        id: crypto.randomUUID(),
        conversation_id: data.conversation_id,
        role: 'agent',
        content: data.response,
        citations: data.citations,
        created_at: new Date().toISOString(),
      }
      setMessages((prev) => [...prev, agentMessage])
      setStreamingContent('')

      // Invalidate queries
      queryClient.invalidateQueries({ queryKey: ['conversations'] })
      queryClient.invalidateQueries({
        queryKey: ['conversation', data.conversation_id],
      })

      // Update URL if new conversation
      if (!conversationId && data.conversation_id) {
        window.history.pushState(
          {},
          '',
          `/?conversation=${data.conversation_id}`
        )
      }
    },
    onError: (error) => {
      console.error('Chat error:', error)
      setIsStreaming(false)
      setStreamingContent('')
    },
  })

  const handleSend = async () => {
    if (!input.trim() || sendMutation.isPending || isStreaming) return

    const userMessage: Message = {
      id: crypto.randomUUID(),
      conversation_id: conversationId || '',
      role: 'user',
      content: input.trim(),
      citations: [],
      created_at: new Date().toISOString(),
    }

    setMessages((prev) => [...prev, userMessage])
    setInput('')

    await sendMutation.mutateAsync({
      message: input.trim(),
      conversation_id: conversationId,
    })
  }

  const handleKeyPress = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  // Auto-scroll to bottom
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight
    }
  }, [messages, streamingContent])

  // Load messages when conversation changes
  useEffect(() => {
    if (conversation) {
      // In production, fetch actual messages
      // For now, just reset
      setMessages([])
    } else {
      setMessages([])
    }
  }, [conversation])

  return (
    <div className="flex-1 flex flex-col h-full">
      {/* Messages area */}
      <ScrollArea className="flex-1 p-6" ref={scrollRef}>
        {isLoading ? (
          <div className="space-y-4">
            {[...Array(3)].map((_, i) => (
              <Skeleton key={i} className="h-24 w-full" />
            ))}
          </div>
        ) : messages.length === 0 && !streamingContent ? (
          <div className="flex items-center justify-center h-full text-center">
            <div>
              <h2 className="text-2xl font-semibold mb-2">
                Welcome to Enterprise Agent Platform
              </h2>
              <p className="text-muted-foreground">
                Ask a question to get started
              </p>
            </div>
          </div>
        ) : (
          <div className="max-w-4xl mx-auto">
            {messages.map((message) => (
              <ChatMessage key={message.id} message={message} />
            ))}

            {/* Streaming message */}
            {isStreaming && streamingContent && (
              <ChatMessage
                message={{
                  id: 'streaming',
                  conversation_id: conversationId || '',
                  role: 'agent',
                  content: streamingContent,
                  citations: [],
                  created_at: new Date().toISOString(),
                }}
              />
            )}
          </div>
        )}
      </ScrollArea>

      {/* Input bar */}
      <div className="border-t border-border p-4 bg-card">
        <div className="max-w-4xl mx-auto flex gap-2">
          <Input
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyPress={handleKeyPress}
            placeholder="Type your message..."
            disabled={sendMutation.isPending || isStreaming}
            className="flex-1"
          />
          <Button
            onClick={handleSend}
            disabled={
              !input.trim() || sendMutation.isPending || isStreaming
            }
            size="icon"
          >
            {sendMutation.isPending || isStreaming ? (
              <Loader2 className="w-5 h-5 animate-spin" />
            ) : (
              <Send className="w-5 h-5" />
            )}
          </Button>
        </div>
        <p className="text-xs text-muted-foreground text-center mt-2">
          Responses are AI-generated. Verify critical information.
        </p>
      </div>
    </div>
  )
}
