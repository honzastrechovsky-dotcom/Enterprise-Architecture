{{/*
Expand the name of the chart.
*/}}
{{- define "enterprise-agent-platform.name" -}}
{{- default .Chart.Name .Values.nameOverride | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Create a default fully qualified app name.
*/}}
{{- define "enterprise-agent-platform.fullname" -}}
{{- if .Values.fullnameOverride }}
{{- .Values.fullnameOverride | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- $name := default .Chart.Name .Values.nameOverride }}
{{- if contains $name .Release.Name }}
{{- .Release.Name | trunc 63 | trimSuffix "-" }}
{{- else }}
{{- printf "%s-%s" .Release.Name $name | trunc 63 | trimSuffix "-" }}
{{- end }}
{{- end }}
{{- end }}

{{/*
Create chart name and version as used by the chart label.
*/}}
{{- define "enterprise-agent-platform.chart" -}}
{{- printf "%s-%s" .Chart.Name .Chart.Version | replace "+" "_" | trunc 63 | trimSuffix "-" }}
{{- end }}

{{/*
Common labels
*/}}
{{- define "enterprise-agent-platform.labels" -}}
helm.sh/chart: {{ include "enterprise-agent-platform.chart" . }}
{{ include "enterprise-agent-platform.selectorLabels" . }}
{{- if .Chart.AppVersion }}
app.kubernetes.io/version: {{ .Chart.AppVersion | quote }}
{{- end }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
{{- end }}

{{/*
Selector labels
*/}}
{{- define "enterprise-agent-platform.selectorLabels" -}}
app.kubernetes.io/name: {{ include "enterprise-agent-platform.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end }}

{{/*
API component labels
*/}}
{{- define "enterprise-agent-platform.api.labels" -}}
{{ include "enterprise-agent-platform.labels" . }}
app.kubernetes.io/component: api
{{- end }}

{{/*
API selector labels
*/}}
{{- define "enterprise-agent-platform.api.selectorLabels" -}}
{{ include "enterprise-agent-platform.selectorLabels" . }}
app.kubernetes.io/component: api
{{- end }}

{{/*
Worker component labels
*/}}
{{- define "enterprise-agent-platform.worker.labels" -}}
{{ include "enterprise-agent-platform.labels" . }}
app.kubernetes.io/component: worker
{{- end }}

{{/*
Worker selector labels
*/}}
{{- define "enterprise-agent-platform.worker.selectorLabels" -}}
{{ include "enterprise-agent-platform.selectorLabels" . }}
app.kubernetes.io/component: worker
{{- end }}

{{/*
Frontend component labels
*/}}
{{- define "enterprise-agent-platform.frontend.labels" -}}
{{ include "enterprise-agent-platform.labels" . }}
app.kubernetes.io/component: frontend
{{- end }}

{{/*
Frontend selector labels
*/}}
{{- define "enterprise-agent-platform.frontend.selectorLabels" -}}
{{ include "enterprise-agent-platform.selectorLabels" . }}
app.kubernetes.io/component: frontend
{{- end }}

{{/*
Create the name of the service account to use
*/}}
{{- define "enterprise-agent-platform.serviceAccountName" -}}
{{- if .Values.serviceAccount.create }}
{{- default (include "enterprise-agent-platform.fullname" .) .Values.serviceAccount.name }}
{{- else }}
{{- default "default" .Values.serviceAccount.name }}
{{- end }}
{{- end }}

{{/*
Database connection URL
*/}}
{{- define "enterprise-agent-platform.databaseUrl" -}}
{{- if .Values.postgresql.enabled }}
postgresql+asyncpg://{{ .Values.postgresql.auth.username }}:{{ .Values.postgresql.auth.password }}@{{ include "enterprise-agent-platform.fullname" . }}-postgresql:5432/{{ .Values.postgresql.auth.database }}
{{- else }}
{{- .Values.config.externalDatabaseUrl }}
{{- end }}
{{- end }}

{{/*
Redis connection URL
*/}}
{{- define "enterprise-agent-platform.redisUrl" -}}
{{- if .Values.redis.enabled }}
redis://{{ include "enterprise-agent-platform.fullname" . }}-redis-master:6379/0
{{- else }}
{{- .Values.config.externalRedisUrl }}
{{- end }}
{{- end }}

{{/*
LiteLLM base URL
*/}}
{{- define "enterprise-agent-platform.litellmBaseUrl" -}}
{{- if .Values.litellm.enabled }}
http://{{ include "enterprise-agent-platform.fullname" . }}-litellm:{{ .Values.litellm.port }}
{{- else }}
{{- .Values.config.externalLitellmUrl }}
{{- end }}
{{- end }}
