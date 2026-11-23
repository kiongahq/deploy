{{- /*
Settings the gateway and the operator share for applications, functions and
model endpoints on Kubernetes. Names follow the operator's LoadWorkloadConfig
and model deployment settings (kiongahq/platform). Feature flags are explicit
("true"/"false") so a component never infers a feature from a missing
variable; the gateway answers 409 not_configured for a disabled feature.
*/ -}}
{{- define "kionga.runtimeEnv" -}}
{{- $v := .Values -}}
{{- $apps := $v.applications -}}
- {name: KIONGA_APPLICATIONS_ENABLED, value: {{ $apps.enabled | toString | quote }}}
- {name: KIONGA_APPLICATIONS_NAMESPACE, value: {{ $v.workspaceNamespace | quote }}}
- {name: KIONGA_INGRESS_CLASS, value: {{ $v.ingress.className | quote }}}
{{- if $apps.enabled }}
- {name: KIONGA_APPLICATIONS_BASE_DOMAIN, value: {{ $apps.baseDomain | quote }}}
- {name: KIONGA_APPLICATIONS_TLS_SECRET, value: {{ $apps.tls.secretName | quote }}}
- {name: KIONGA_APPLICATIONS_INGRESS_ANNOTATIONS, value: {{ $apps.ingressAnnotations | toJson | quote }}}
# visibility=sso hosts go through the gateway, which signs the caller in.
- {name: KIONGA_SSO_UPSTREAM, value: {{ printf "%s-gateway.%s.svc.cluster.local:8080" .Release.Name .Release.Namespace | quote }}}
- {name: KIONGA_BUILDKIT_IMAGE, value: {{ $apps.build.image | quote }}}
{{- if $apps.registry.url }}
- {name: KIONGA_BUILD_REGISTRY, value: {{ $apps.registry.url | quote }}}
- {name: KIONGA_BUILD_PUSH_SECRET, value: {{ $apps.registry.pushSecretName | quote }}}
- {name: KIONGA_BUILD_REGISTRY_INSECURE, value: {{ $apps.registry.insecure | toString | quote }}}
- {name: KIONGA_BUILD_TIMEOUT_SECONDS, value: {{ $apps.build.timeoutSeconds | toString | quote }}}
- {name: KIONGA_BUILD_RESOURCES, value: {{ $apps.build.resources | toJson | quote }}}
{{- end }}
{{- end }}
- {name: KIONGA_FUNCTIONS_ENABLED, value: {{ $v.functions.enabled | toString | quote }}}
{{- if $v.functions.enabled }}
- {name: KIONGA_FUNCTIONS_SCALEDOWN_SECONDS, value: {{ $v.functions.scaleDownSeconds | toString | quote }}}
- {name: KIONGA_FUNCTIONS_MAX_REPLICAS, value: {{ $v.functions.defaultMaxReplicas | toString | quote }}}
{{- end }}
- {name: KIONGA_KEDA_ENABLED, value: {{ $v.keda.enabled | toString | quote }}}
{{- if $v.keda.enabled }}
- {name: KIONGA_KEDA_NAMESPACE, value: {{ $v.keda.namespace | quote }}}
- {name: KIONGA_KEDA_HTTP_INTERCEPTOR, value: {{ printf "%s.%s.svc.cluster.local:%v" $v.keda.interceptor.service $v.keda.namespace $v.keda.interceptor.port | quote }}}
- {name: KIONGA_KEDA_INTERCEPTOR_URL, value: {{ printf "http://%s.%s.svc.cluster.local:%v" $v.keda.interceptor.service $v.keda.namespace $v.keda.interceptor.port | quote }}}
{{- end }}
- {name: KIONGA_KSERVE_ENABLED, value: {{ $v.kserve.enabled | toString | quote }}}
{{- if $v.kserve.enabled }}
- {name: KIONGA_KSERVE_DEPLOYMENT_MODE, value: {{ $v.kserve.deploymentMode | quote }}}
- {name: KIONGA_MODEL_STORAGE_SECRET, value: {{ $v.kserve.storageSecretName | quote }}}
{{- with $v.kserve.storage }}
# Empty endpoint: AWS S3. USE_HTTPS is "1" or "0".
- {name: KIONGA_MODEL_S3_ENDPOINT, value: {{ .endpoint | default "" | quote }}}
- {name: KIONGA_MODEL_S3_USE_HTTPS, value: {{ ternary "1" "0" (.useHTTPS | default false) | quote }}}
- {name: KIONGA_MODEL_S3_REGION, value: {{ .region | default "us-east-1" | quote }}}
- {name: KIONGA_MLFLOW_ARTIFACTS_DESTINATION, value: {{ .artifactsDestination | default "" | quote }}}
{{- end }}
{{- end }}
{{- if $v.website.url }}
- {name: KIONGA_WEBSITE_URL, value: {{ $v.website.url | quote }}}
{{- end }}
- {name: KIONGA_BLOG_AUTHORING_ENABLED, value: {{ $v.blog.authoringEnabled | toString | quote }}}
{{- end }}
