# Bindings

`bindings/<owner>/<repo>.yaml` (`binding/v1`) links an app to the Azure identity security's process created for it and to the
Databricks connection analytics' process granted to that identity. The portal's writer writes these files when an app's admin
picks them on the app's page; nothing else should. The release renders them into the app's signed manifests as an Azure workload
identity and plain environment variables (no secrets). `factory-check` refuses a binding without the app's intake record.
