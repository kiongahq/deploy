{{- /*
Settings the gateway and the operator share for applications, functions and
model endpoints on Kubernetes. Every flag is explicit ("true"/"false") so a
component never infers a feature from a missing variable. The gateway answers
409 not_configured, with the enablement steps, for a disabled feature.
*/ -}}
{{- define "kionga.runtimeEnv" -}}
{{- $v := .Values -}}
{{- $apps := $v.applications -}}
- {name: KIONGA_APPLICATIONS_ENABLED, value: {{ $apps.enabled | toString | quote }}}
- {name: KIONGA_APPLICATIONS_NAMESPACE, value: {{ $v.workspaceNamespace | quote }}}
{{- if $apps.enabled }}
- {name: KIONGA_APPLICATIONS_BASE_DOMAIN, value: {{ $apps.baseDomain | quote }}}
- {name: KIONGA_APPLICATIONS_TLS_SECRET, value: {{ $apps.tls.secretName | quote }}}
- {name: KIONGA_APPLICATIONS_INGRESS_CLASS, value: {{ $v.ingress.className | quote }}}
- {name: KIONGA_APPLICATIONS_INGRESS_ANNOTATIONS, value: {{ $apps.ingressAnnotations | toJson | quote }}}
- {name: KIONGA_APPLICATIONS_SSO_URL, value: {{ printf "https://%s" $v.ingress.host | quote }}}
- {name: KIONGA_APPLICATIONS_BUILDS_ENABLED, value: {{ ne (toString $apps.registry.url) "" | toString | quote }}}
{{- if $apps.registry.url }}
- {name: KIONGA_APPLICATIONS_REGISTRY, value: {{ $apps.registry.url | quote }}}
- {name: KIONGA_APPLICATIONS_REGISTRY_SECRET, value: {{ $apps.registry.pushSecretName | quote }}}
- {name: KIONGA_APPLICATIONS_REGISTRY_INSECURE, value: {{ $apps.registry.insecure | toString | quote }}}
- {name: KIONGA_BUILD_NAMESPACE, value: {{ $apps.build.namespace | quote }}}
- {name: KIONGA_BUILDKIT_IMAGE, value: {{ $apps.build.image | quote }}}
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
- {name: KIONGA_KEDA_INTERCEPTOR_URL, value: {{ printf "http://%s.%s.svc:%v" $v.keda.interceptor.service $v.keda.namespace $v.keda.interceptor.port | quote }}}
{{- end }}
- {name: KIONGA_KSERVE_ENABLED, value: {{ $v.kserve.enabled | toString | quote }}}
{{- if $v.kserve.enabled }}
- {name: KIONGA_KSERVE_DEPLOYMENT_MODE, value: {{ $v.kserve.deploymentMode | quote }}}
- {name: KIONGA_KSERVE_STORAGE_SECRET, value: {{ $v.kserve.storageSecretName | quote }}}
{{- end }}
{{- if $v.website.url }}
- {name: KIONGA_WEBSITE_URL, value: {{ $v.website.url | quote }}}
{{- end }}
{{- end }}
