{{/* The name of every object: the release name, or fullnameOverride. It is also the Service name. */}}
{{- define "mimir.fullname" -}}
{{- default .Release.Name .Values.fullnameOverride | trunc 63 | trimSuffix "-" -}}
{{- end }}

{{/* Labels that select the pod. Never add to them: a Deployment selector can not change after the first deploy. */}}
{{- define "mimir.selectorLabels" -}}
app.kubernetes.io/name: {{ .Chart.Name }}
{{- end }}

{{/* All labels on the objects. */}}
{{- define "mimir.labels" -}}
{{ include "mimir.selectorLabels" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
app.kubernetes.io/version: {{ .Values.image.tag | quote }}
app.kubernetes.io/managed-by: {{ .Release.Service }}
helm.sh/chart: {{ printf "%s-%s" .Chart.Name .Chart.Version }}
{{- end }}

{{/* The HTTP port is set once, in config.server.http_listen_port. */}}
{{- define "mimir.httpPort" -}}
{{- .Values.config.server.http_listen_port -}}
{{- end }}

{{/* -target=a,b,c from the modules list. An empty list is a mistake: Mimir would start the default target. */}}
{{- define "mimir.target" -}}
{{- required "values.modules must list at least one module (for example [all])" (join "," .Values.modules) -}}
{{- end }}
