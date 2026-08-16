# NetBox deployment

The example deployment uses the official NetBox Community chart 8.3.37
(NetBox 4.6.5). Replace the example hostname and storage class in
`values.yaml` before deployment.

Secrets are generated directly in Kubernetes and are not stored in this
directory. The controller uses a dedicated NetBox API token stored in the
`netbox-ironic-controller-netbox` Secret.

After changing the NetBox `admin` password in the UI, synchronize the Helm
release and Kubernetes Secrets without exposing it on the command line:

```bash
./deploy/netbox/sync-admin-password.sh
```
