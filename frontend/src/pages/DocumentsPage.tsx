/**
 * DocumentsPage - Document management interface.
 *
 * Features:
 * - Upload dropzone
 * - Document list with status
 * - Classification display
 * - Delete with confirmation
 * - Search/filter
 */

import { useState } from 'react'
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import {
  FileText,
  Trash2,
  Search,
  CheckCircle2,
  AlertCircle,
  Loader2,
} from 'lucide-react'
import { format } from 'date-fns'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Card, CardHeader, CardTitle, CardContent } from '@/components/ui/card'
import { Badge } from '@/components/ui/badge'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Skeleton } from '@/components/ui/skeleton'
import { DocumentUpload } from '@/components/DocumentUpload'
import { api } from '@/lib/api'
import type { Document, DocumentStatus } from '@/types'

const STATUS_CONFIG: Record<
  DocumentStatus,
  { icon: any; color: string; label: string }
> = {
  uploading: {
    icon: Loader2,
    color: 'text-blue-500',
    label: 'Uploading',
  },
  processing: {
    icon: Loader2,
    color: 'text-yellow-500',
    label: 'Processing',
  },
  ready: {
    icon: CheckCircle2,
    color: 'text-green-500',
    label: 'Ready',
  },
  error: {
    icon: AlertCircle,
    color: 'text-red-500',
    label: 'Error',
  },
}

export function DocumentsPage() {
  const [searchQuery, setSearchQuery] = useState('')
  const [filterStatus, setFilterStatus] = useState<DocumentStatus | 'all'>('all')
  const queryClient = useQueryClient()

  const { data: documents, isLoading } = useQuery({
    queryKey: ['documents'],
    queryFn: () => api.getDocuments(),
  })

  const deleteMutation = useMutation({
    mutationFn: (id: string) => api.deleteDocument(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['documents'] })
    },
  })

  const handleDelete = (doc: Document) => {
    if (
      confirm(`Are you sure you want to delete "${doc.filename}"? This cannot be undone.`)
    ) {
      deleteMutation.mutate(doc.id)
    }
  }

  const filteredDocuments = documents?.filter((doc: Document) => {
    const matchesSearch =
      doc.filename.toLowerCase().includes(searchQuery.toLowerCase()) ||
      doc.file_type.toLowerCase().includes(searchQuery.toLowerCase())
    const matchesStatus = filterStatus === 'all' || doc.status === filterStatus
    return matchesSearch && matchesStatus
  })

  return (
    <div className="flex-1 p-6 overflow-auto">
      <div className="max-w-6xl mx-auto space-y-6">
        <div>
          <h1 className="text-3xl font-bold mb-2">Documents</h1>
          <p className="text-muted-foreground">
            Upload and manage your organization's documents
          </p>
        </div>

        {/* Upload section */}
        <DocumentUpload
          onUploadComplete={() => {
            queryClient.invalidateQueries({ queryKey: ['documents'] })
          }}
        />

        {/* Filters */}
        <div className="flex gap-4">
          <div className="flex-1 relative">
            <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 w-4 h-4 text-muted-foreground" />
            <Input
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder="Search documents..."
              className="pl-10"
            />
          </div>
          <select
            value={filterStatus}
            onChange={(e) => setFilterStatus(e.target.value as DocumentStatus | 'all')}
            className="px-4 py-2 rounded-md border border-input bg-background text-sm"
          >
            <option value="all">All Status</option>
            <option value="ready">Ready</option>
            <option value="processing">Processing</option>
            <option value="error">Error</option>
          </select>
        </div>

        {/* Documents list */}
        <Card>
          <CardHeader>
            <CardTitle>Your Documents</CardTitle>
          </CardHeader>
          <CardContent>
            {isLoading ? (
              <div className="space-y-3">
                {[...Array(5)].map((_, i) => (
                  <Skeleton key={i} className="h-20 w-full" />
                ))}
              </div>
            ) : filteredDocuments && filteredDocuments.length > 0 ? (
              <ScrollArea className="h-[600px]">
                <div className="space-y-3">
                  {filteredDocuments.map((doc: Document) => {
                    const StatusIcon = STATUS_CONFIG[doc.status].icon
                    return (
                      <div
                        key={doc.id}
                        className="flex items-center gap-4 p-4 rounded-md border border-border hover:bg-accent/50 transition-colors"
                      >
                        <FileText className="w-10 h-10 text-primary flex-shrink-0" />

                        <div className="flex-1 min-w-0">
                          <div className="flex items-center gap-2 mb-1">
                            <h3 className="font-medium truncate">
                              {doc.filename}
                            </h3>
                            <Badge variant="outline">{doc.classification}</Badge>
                          </div>

                          <div className="flex items-center gap-4 text-sm text-muted-foreground">
                            <span className="flex items-center gap-1">
                              <StatusIcon
                                className={`w-4 h-4 ${STATUS_CONFIG[doc.status].color} ${
                                  doc.status === 'processing' || doc.status === 'uploading'
                                    ? 'animate-spin'
                                    : ''
                                }`}
                              />
                              {STATUS_CONFIG[doc.status].label}
                            </span>
                            <span>
                              {(doc.file_size_bytes / 1024 / 1024).toFixed(2)} MB
                            </span>
                            <span>
                              {format(new Date(doc.uploaded_at), 'MMM d, yyyy')}
                            </span>
                            {doc.chunk_count > 0 && (
                              <span>{doc.chunk_count} chunks</span>
                            )}
                          </div>

                          {doc.error_message && (
                            <div className="mt-2 text-sm text-destructive">
                              Error: {doc.error_message}
                            </div>
                          )}
                        </div>

                        <Button
                          variant="ghost"
                          size="icon"
                          onClick={() => handleDelete(doc)}
                          disabled={deleteMutation.isPending}
                        >
                          <Trash2 className="w-4 h-4 text-destructive" />
                        </Button>
                      </div>
                    )
                  })}
                </div>
              </ScrollArea>
            ) : (
              <div className="text-center py-12 text-muted-foreground">
                {searchQuery || filterStatus !== 'all'
                  ? 'No documents match your filters'
                  : 'No documents uploaded yet'}
              </div>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  )
}
