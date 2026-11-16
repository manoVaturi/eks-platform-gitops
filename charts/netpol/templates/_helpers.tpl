{{/*
The DNS rule every egress policy needs: CoreDNS in kube-system, on port 53 over UDP and TCP.
The namespaceSelector and the podSelector sit in ONE "to" item on purpose: both must match.
*/}}
{{- define "netpol.dnsEgress" -}}
- to:
    - namespaceSelector:
        matchLabels:
          kubernetes.io/metadata.name: kube-system
      podSelector:
        matchLabels:
          k8s-app: kube-dns
  ports:
    - protocol: UDP
      port: 53
    - protocol: TCP
      port: 53
{{- end }}
