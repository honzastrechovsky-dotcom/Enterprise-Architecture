/**
 * DocumentUpload component - drag-and-drop file upload with classification.
 *
 * Features:
 * - Drag and drop zone
 * - File type validation (whitelist from backend)
 * - Classification selector
 * - Upload progress tracking
 * - Error handling
 */

import { useState, useCallback } from 'react'
import { Upload, FileText, AlertCircle, CheckCircle2 } from 'lucide-react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { cn } from '@/lib/utils'
import { api } from '@/lib/api'
import type { DocumentClassification } from '@/types'

const ALLOWED_TYPES = [
  '.txt',
  '.pdf',
  '.docx',
  '.doc',
  '.xlsx',
  '.xls',
  '.pptx',
  '.ppt',
  '.md',
  '.csv',
]

const CLASSIFICATIONS: Array<{
  value: DocumentClassification
  label: string
  description: string
}> = [
  {
    value: 'CLASS_I',
    label: 'Class I',
    description: 'Public - No restrictions',
  },
  {
    value: 'CLASS_II',
    label: 'Class II',
    description: 'Internal - Company confidential',
  },
  {
    value: 'CLASS_III',
    label: 'Class III',
    description: 'Restricted - Need to know',
  },
  {
    value: 'CLASS_IV',
    label: 'Class IV',
    description: 'Highly Restricted - Minimal access',
  },
]

interface DocumentUploadProps {
  onUploadComplete?: () => void
}

export function DocumentUpload({ onUploadComplete }: DocumentUploadProps) {
  const [dragActive, setDragActive] = useState(false)
  const [selectedFile, setSelectedFile] = useState<File | null>(null)
  const [classification, setClassification] =
    useState<DocumentClassification>('CLASS_II')
  const queryClient = useQueryClient()

  const uploadMutation = useMutation({
    mutationFn: (file: File) => api.uploadDocument(file, classification),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['documents'] })
      setSelectedFile(null)
      onUploadComplete?.()
    },
  })

  const handleDrag = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    e.stopPropagation()
    if (e.type === 'dragenter' || e.type === 'dragover') {
      setDragActive(true)
    } else if (e.type === 'dragleave') {
      setDragActive(false)
    }
  }, [])

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault()
    e.stopPropagation()
    setDragActive(false)

    if (e.dataTransfer.files && e.dataTransfer.files[0]) {
      handleFile(e.dataTransfer.files[0])
    }
  }, [])

  const handleChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    e.preventDefault()
    if (e.target.files && e.target.files[0]) {
      handleFile(e.target.files[0])
    }
  }

  const handleFile = (file: File) => {
    const ext = '.' + file.name.split('.').pop()?.toLowerCase()
    if (!ALLOWED_TYPES.includes(ext)) {
      alert(`File type ${ext} is not allowed. Allowed types: ${ALLOWED_TYPES.join(', ')}`)
      return
    }
    setSelectedFile(file)
  }

  const handleUpload = () => {
    if (selectedFile) {
      uploadMutation.mutate(selectedFile)
    }
  }

  const handleCancel = () => {
    setSelectedFile(null)
    uploadMutation.reset()
  }

  return (
    <Card className="p-6">
      {/* Classification selector */}
      <div className="mb-4">
        <label className="text-sm font-medium mb-2 block">
          Classification Level
        </label>
        <div className="grid grid-cols-2 gap-2">
          {CLASSIFICATIONS.map((cls) => (
            <button
              key={cls.value}
              onClick={() => setClassification(cls.value)}
              className={cn(
                'p-3 rounded-md border text-left transition-colors',
                classification === cls.value
                  ? 'border-primary bg-primary/10'
                  : 'border-border hover:bg-accent'
              )}
            >
              <div className="font-medium text-sm">{cls.label}</div>
              <div className="text-xs text-muted-foreground">
                {cls.description}
              </div>
            </button>
          ))}
        </div>
      </div>

      {/* Drop zone */}
      {!selectedFile && (
        <div
          className={cn(
            'border-2 border-dashed rounded-lg p-8 text-center transition-colors cursor-pointer',
            dragActive
              ? 'border-primary bg-primary/5'
              : 'border-border hover:border-primary/50'
          )}
          onDragEnter={handleDrag}
          onDragLeave={handleDrag}
          onDragOver={handleDrag}
          onDrop={handleDrop}
          onClick={() => document.getElementById('file-input')?.click()}
        >
          <Upload className="w-12 h-12 mx-auto mb-4 text-muted-foreground" />
          <p className="text-sm font-medium mb-1">
            Drop file here or click to browse
          </p>
          <p className="text-xs text-muted-foreground">
            Supported: {ALLOWED_TYPES.join(', ')}
          </p>
          <input
            id="file-input"
            type="file"
            className="hidden"
            onChange={handleChange}
            accept={ALLOWED_TYPES.join(',')}
          />
        </div>
      )}

      {/* Selected file */}
      {selectedFile && (
        <div className="space-y-4">
          <div className="flex items-center gap-3 p-4 rounded-md bg-accent">
            <FileText className="w-8 h-8 text-primary" />
            <div className="flex-1 min-w-0">
              <div className="font-medium truncate">{selectedFile.name}</div>
              <div className="text-sm text-muted-foreground">
                {(selectedFile.size / 1024 / 1024).toFixed(2)} MB
              </div>
            </div>
          </div>

          {uploadMutation.isPending && (
            <div className="flex items-center gap-2 text-sm text-muted-foreground">
              <div className="animate-spin h-4 w-4 border-2 border-primary border-t-transparent rounded-full" />
              Uploading...
            </div>
          )}

          {uploadMutation.isError && (
            <div className="flex items-center gap-2 p-3 rounded-md bg-destructive/10 text-destructive">
              <AlertCircle className="w-5 h-5" />
              <span className="text-sm">
                {uploadMutation.error instanceof Error
                  ? uploadMutation.error.message
                  : 'Upload failed'}
              </span>
            </div>
          )}

          {uploadMutation.isSuccess && (
            <div className="flex items-center gap-2 p-3 rounded-md bg-green-500/10 text-green-500">
              <CheckCircle2 className="w-5 h-5" />
              <span className="text-sm">Upload successful!</span>
            </div>
          )}

          <div className="flex gap-2">
            <Button
              onClick={handleUpload}
              disabled={uploadMutation.isPending}
              className="flex-1"
            >
              Upload
            </Button>
            <Button
              variant="outline"
              onClick={handleCancel}
              disabled={uploadMutation.isPending}
            >
              Cancel
            </Button>
          </div>
        </div>
      )}
    </Card>
  )
}
