# Production metrics communication

- The application keeps its project-default network and existing external `connection-machine-data` volume.
- Its metrics server listens on the container interface, but production Compose publishes no host port.
- The producer joins the dedicated external, internal Docker network `connection-machine-metrics` with DNS alias `connection-machine-metrics`.
- Prometheus in `artbred/misc` joins that same network and scrapes `connection-machine-metrics:9102`. No container IP, host IP or public metrics port is required.
- Dokploy owns creation of this shared network. Create it with the network API as bridge/internal/attachable before deploying either dependent application; do not silently substitute a broad shared ingress network.
- Deploy this producer through its existing Dokploy record/project, verify readiness and named metrics access, then deploy the coordinated monitoring consumer. Preserve environment values and the data volume; do not recreate the application under another project identity.
- The explicit DNS resolver in the existing configuration is resolver infrastructure, not an application upstream. The network contract concerns service destinations and preserves unrelated bot behavior.

Run the source contract with `python -m unittest discover -s tests -p test_network_contract.py -v`. The test uses a temporary interpolation fixture and native Compose rendering; it does not start containers or contact LinkedIn.
