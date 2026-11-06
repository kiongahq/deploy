# Changelog

## [0.1.1](https://github.com/kiongahq/deploy/compare/v0.1.0...v0.1.1) (2026-11-06)


### Features

* add the sandbox manager to Compose and Helm ([#56](https://github.com/kiongahq/deploy/issues/56)) ([5fd24ec](https://github.com/kiongahq/deploy/commit/5fd24ec5ebfa5916a8291e102db785dc7e6bed91))
* **agents:** real agentic AI layer — LangGraph runtime, SDK primitives, gateway invoke path ([848363e](https://github.com/kiongahq/deploy/commit/848363e201a3bd9c92c4d16c0940b77e25469259))
* **agents:** support Agno, NOOA, and extensible framework adapters ([fe6dd7c](https://github.com/kiongahq/deploy/commit/fe6dd7cb81275800e13c62e6945139a216dd7064))
* **auth:** add provisioned user RBAC and quotas ([1ded63b](https://github.com/kiongahq/deploy/commit/1ded63be870839d755849fb50dda7246a4acdd33))
* **auth:** require login for console access ([ea8adae](https://github.com/kiongahq/deploy/commit/ea8adae256ad044389ccb347df2c4ee606acb3ad))
* complete Kionga platform foundation ([5fcfbd3](https://github.com/kiongahq/deploy/commit/5fcfbd347fde13ef4913c7a5f548ab24a1ad93f2))
* **compose:** isolated Agent Designer runner ([77634b1](https://github.com/kiongahq/deploy/commit/77634b1694a5bb63b36522aefa1c501013a10cd9))
* configure Langflow agent runtime images and CRD artifacts ([170ffed](https://github.com/kiongahq/deploy/commit/170ffed7f44ee58f7ddbf7d6bfef00f41a5fcdeb))
* **console:** add metadata drilldowns and mounted notebook storage ([7a908ca](https://github.com/kiongahq/deploy/commit/7a908ca8ce7e19485a5b2258c7d0c1a2b3715dba))
* **data-plane:** real feature store, object browsing, Langfuse, live catalog ([fbadb5b](https://github.com/kiongahq/deploy/commit/fbadb5ba985a65fb1a867a7878fc0d1e20d8951d))
* **deploy:** add EKS GKE and AKS staging preflight ([90f1f2f](https://github.com/kiongahq/deploy/commit/90f1f2f64b4f57e8a829e7eb95ce3b78cd650aa7))
* **deploy:** add managed production packages and isolated workspaces ([daafbb2](https://github.com/kiongahq/deploy/commit/daafbb23ebf442b0138fd98996d602f056352907))
* **deploy:** configure GitHub repository integration ([b56f65c](https://github.com/kiongahq/deploy/commit/b56f65c32bc2dae9a0519b16a4110207cc9f4bd9))
* **deploy:** private image registries (GHCR packages are private) ([7ff9528](https://github.com/kiongahq/deploy/commit/7ff9528219edc7fb7d8499fe44bbca71032ee844))
* **deploy:** support Podman for local Kionga stacks ([0bb8c4c](https://github.com/kiongahq/deploy/commit/0bb8c4caae286d6c46313c070d311fd4e2a5f288))
* **execution:** real pipelines, model serving, serverless, and real-time processing ([103723b](https://github.com/kiongahq/deploy/commit/103723bf59e8becb34b6383caa97dda71da9e11b))
* **helm:** opt-in Agent Designer runner with its own network fence ([1c30194](https://github.com/kiongahq/deploy/commit/1c30194244df5cf0f845b709eddc50f92e4295b9))
* **hosting:** public single-VM deployment — same stack, TLS + OIDC edge ([4900fa1](https://github.com/kiongahq/deploy/commit/4900fa1af9e2e3783feb603dc50a2b238165b094))
* integrate Hugging Face and secure project workspace workflows ([7975cdb](https://github.com/kiongahq/deploy/commit/7975cdbef8638733303300cf042b2d4e63da1804))
* **integrations:** connect standard MLOps service APIs ([0feb042](https://github.com/kiongahq/deploy/commit/0feb0423436d9a584c0044cbeb897f18580212f3))
* **k8s:** run container pipeline nodes as hardened Kubernetes Jobs (opt-in) ([a77f125](https://github.com/kiongahq/deploy/commit/a77f125108a0d41094a2ace5748a0fade84d5c9d))
* **local-up:** heal the engine, move busy ports, build only what is missing ([055e811](https://github.com/kiongahq/deploy/commit/055e81159936fc55799c1aed705f40f3325a4ba3))
* **operator:** define platform CRDs and agent reconciliation ([b90f9b0](https://github.com/kiongahq/deploy/commit/b90f9b0798c40560fcd169a38d3296135f8c5b80))
* **operator:** reconcile provisioned user workspaces ([8566013](https://github.com/kiongahq/deploy/commit/85660134761f0d8bf157523ca460f59224018b7f))
* **operator:** run leader-elected Kubernetes reconciliation manager ([7d387e0](https://github.com/kiongahq/deploy/commit/7d387e017bfe4c673bca4da00a206bf1859a5783))
* **operator:** run leader-elected Kubernetes reconciliation manager ([de2f516](https://github.com/kiongahq/deploy/commit/de2f516ea58f6be98608502b0655ccbec84f8ca7))
* **pipelines:** manual run preflight, structured logs and log export ([63b17d6](https://github.com/kiongahq/deploy/commit/63b17d62a0be8133e752e7e9c929774a80a9aba2))
* **platform:** complete lifecycle integration and local reliability stack ([14d2d21](https://github.com/kiongahq/deploy/commit/14d2d218c325af7b152ffc7b3a478bfed2e9936f))
* **product:** ship operational workspace and production hardening ([f620003](https://github.com/kiongahq/deploy/commit/f62000300250d2586adb24a74701c73624e0267e))
* project keys and a server-side provider key for the trace proxy ([#53](https://github.com/kiongahq/deploy/issues/53)) ([b501ec5](https://github.com/kiongahq/deploy/commit/b501ec5f5433c025e64052ff917d8e558058b1dd))
* provision isolated Langflow workspaces in Compose and Kubernetes ([ec5787f](https://github.com/kiongahq/deploy/commit/ec5787f8e16cea185e16953991d889b41715fcfc))
* **rbac:** role-based access control end to end + fully functional console ([13335fb](https://github.com/kiongahq/deploy/commit/13335fb4383177d3e210b2edbcc29181bc565bc8))
* **release:** add reviewed versions and consistent publication ([79b0cf0](https://github.com/kiongahq/deploy/commit/79b0cf0eed01c50f73c2990e09b7c95dc0fd2243))
* **release:** standardize versions, changelogs and publication ([d7f9e23](https://github.com/kiongahq/deploy/commit/d7f9e239cf29c03ff460daaf68739f1b137a25f2))
* replace MinIO with RustFS as the bundled object store ([4052a12](https://github.com/kiongahq/deploy/commit/4052a125d1d3f60b8db35da446948e8bdfb37060))
* **security:** add OIDC verification and role-based access control ([91493f5](https://github.com/kiongahq/deploy/commit/91493f52e496b7552f8d958efade5b14548583ae))
* **sso:** local single sign-on overlay with Dex and a test LDAP directory ([c9eb66c](https://github.com/kiongahq/deploy/commit/c9eb66c885e78c95453bbdf90b1b75fa288cc948))
* **sso:** make local-up starts every sign-in and sign-up method ([9439197](https://github.com/kiongahq/deploy/commit/9439197d5f112d287394650ba1565815faac84a3))
* **storage:** add PostgreSQL repository and transactional outbox ([4a68eae](https://github.com/kiongahq/deploy/commit/4a68eae3e9c8f3ca5d6b30d1614f22185b5a06c5))
* **workbench:** Jupyter dev workbench + service connection guide ([f3f3acd](https://github.com/kiongahq/deploy/commit/f3f3acd82ffe930c242643254991df497e0462ee))
* **workspace:** add AI scaffolding and shared browser IDE ([0a332f6](https://github.com/kiongahq/deploy/commit/0a332f6c773d360f5f96a574d93b084ad674c041))
* **workspace:** sidecar scaffold-jobs endpoint with argv allowlist, scrubbed env and token; expose it in Jupyter via jupyter-server-proxy ([bda5f5c](https://github.com/kiongahq/deploy/commit/bda5f5c801b769ace910f012866e78d4d7b661a4))
* **workspaces:** wire internal identity and remove local-only preflight ([512205c](https://github.com/kiongahq/deploy/commit/512205c61d067f793b5084971dcfa2f61e7d1c69))


### Fixes

* allow local Langflow flow capture through gateway ([dd1dbf4](https://github.com/kiongahq/deploy/commit/dd1dbf4843840587cc0dbf4f9c39a01ba25396b4))
* **compose:** let MLflow start on a new install ([#57](https://github.com/kiongahq/deploy/issues/57)) ([2941e0e](https://github.com/kiongahq/deploy/commit/2941e0e01ebce43f884b759d7a420cc955041cda))
* **deploy:** log-exporter secrets and settings, Docker API pin, Helm extraEnv ([16f9d23](https://github.com/kiongahq/deploy/commit/16f9d2314262adfc1d8d598301283b0a972c5f08))
* include Langflow agent image in production values example ([20c247d](https://github.com/kiongahq/deploy/commit/20c247dce03ba7874694c4aa6a92e3473b880780))
* **load:** sign in once in k6 setup; never pull the locally built materializer image ([a84a218](https://github.com/kiongahq/deploy/commit/a84a2186ca09f6beb3fa9fdf8106c37e6f75a161))
* **local-up:** cancel and retry a pull that stops receiving data ([ae23034](https://github.com/kiongahq/deploy/commit/ae230342dae5ca7b1333429ec827827a83ecac1a))
* **local-up:** move any busy default port, probe the bind address ([9207db2](https://github.com/kiongahq/deploy/commit/9207db291226b78c16a7cc309de668b787cc1d1d))
* **local-up:** show image download progress instead of a silent pull ([132f0b7](https://github.com/kiongahq/deploy/commit/132f0b7f6e1a27b51c6bc31068c57aabf1f94172))
* **local:** bootstrap buckets without mc image and reuse cached builds ([20a15f9](https://github.com/kiongahq/deploy/commit/20a15f93eef1568271513cb9e42c2c6ddfb6abc3))
* **local:** finish RustFS-only startup and verified smoke ([f1aac7b](https://github.com/kiongahq/deploy/commit/f1aac7b0886c317476199c6660bba602fbb9672e))
* **local:** generate workspace tokens and mount a scoped prefix ([#54](https://github.com/kiongahq/deploy/issues/54)) ([88e538c](https://github.com/kiongahq/deploy/commit/88e538c58ab17153fd6c4d981ad8b948c34b3575))
* **local:** isolate integration tests and ship healthy MLflow stack ([f25f1b8](https://github.com/kiongahq/deploy/commit/f25f1b8589979ef64db79604d2625c1779ff174d))
* **local:** print reachable canonical console and workspace URLs ([07b6ce4](https://github.com/kiongahq/deploy/commit/07b6ce4a1ae33fbbde5dd2fa8c8b00042532f02f))
* **migration:** recover unavailable MinIO images ([4cd99fe](https://github.com/kiongahq/deploy/commit/4cd99fee19ac24dcbba3b15f0f3ca6f31d78f1b2))
* **realtime:** survive Kafka REST poll timeouts. A transient ReadTimeout ([f3f3acd](https://github.com/kiongahq/deploy/commit/f3f3acd82ffe930c242643254991df497e0462ee))
* **security:** align MLflow images on patched version 3.17.2 ([2a8939e](https://github.com/kiongahq/deploy/commit/2a8939e5dcad49e1a2231d0991d4bcdd36b4598a))
* **security:** bind local-mode ports to loopback by default ([4231cbd](https://github.com/kiongahq/deploy/commit/4231cbdb686c1b991a7c330eda91343009250683))
* **security:** give storage-proxy, feature-gateway and serving-manager the internal token ([385dc88](https://github.com/kiongahq/deploy/commit/385dc886201202fa74e5f6ef93826c2e8cff17d8))
* **security:** loopback-only ports and no default credentials in public mode ([#52](https://github.com/kiongahq/deploy/issues/52)) ([6c550a8](https://github.com/kiongahq/deploy/commit/6c550a8e5cc82c5e27d82a32cfb8229041bacc89)), closes [#4](https://github.com/kiongahq/deploy/issues/4)
* **security:** MLflow 3.16.1 server; local stack resumes whole ([3000412](https://github.com/kiongahq/deploy/commit/30004124df7471f2c8d9fb601bd92de3265f4d98))
* **smoke:** produce realtime events from the processor container ([212ad4b](https://github.com/kiongahq/deploy/commit/212ad4b51cd4ba20862965d2ce0836a63738fe99))
* **stack:** full-stack smoke green — 18/18 platform capabilities verified live ([cdd1c07](https://github.com/kiongahq/deploy/commit/cdd1c07a65de2a85f9617013aa3ee1c8bbaf04dd))
* **stress:** sign in like the smoke test, and fail when no write happened ([5872aaa](https://github.com/kiongahq/deploy/commit/5872aaa3878f662894fe957eae8fff6f87fcbbca))
* **workspace:** make local IDE launch through current gateway ([4127ec6](https://github.com/kiongahq/deploy/commit/4127ec6cf52d61e1a3666f42712418c7d3af6e84))
* **workspace:** restore Jupyter launch and stop stale images ([2a1654d](https://github.com/kiongahq/deploy/commit/2a1654d7107aeb8ff2a668cc25e4ad30b1c63422))
* **workspaces:** inject governed platform endpoints ([f6821c2](https://github.com/kiongahq/deploy/commit/f6821c24427550a79930dc0af0133388af08ee63))


### Documentation

* **deploy:** explain frontend rebuilds and local gateway origins ([5fb3930](https://github.com/kiongahq/deploy/commit/5fb39305d6357e591652ec687a3235b6bc53612a))
* **editorial:** editor decision record, author/editor/operator guide, API reference; provision kionga-blog-media bucket and gateway media env ([53e9c54](https://github.com/kiongahq/deploy/commit/53e9c547167332181f970d67512430c5f520a30d))
* product README, CONTRIBUTING and ARCHITECTURE ([73cddb4](https://github.com/kiongahq/deploy/commit/73cddb47a546c2aa66c35f349fbb5e79d07f1c31))
* **release:** align contributor publication instructions ([29dd038](https://github.com/kiongahq/deploy/commit/29dd038015b17d7be96b55367619b7a564396df7))
* **security:** clarify sole-maintainer release review ([cc8a58a](https://github.com/kiongahq/deploy/commit/cc8a58a658e6535bceb1531b4d63718de9a714e6))
* use explicit links in deployment guide ([6fdbcbf](https://github.com/kiongahq/deploy/commit/6fdbcbf17f970457dee0e0445c79a0768caa4408))


### Build and dependencies

* **compose:** compile the React frontend in the gateway image ([0016662](https://github.com/kiongahq/deploy/commit/00166629b005abb9ba5857a48cb564547dd93566))
* **kind:** use the dedicated gateway Dockerfile for local clusters ([80bfa23](https://github.com/kiongahq/deploy/commit/80bfa237948575cc422f91e0cf08235e9273cefd))


### Automation

* add pressure-test lane (full-stack + load + concurrency + vuln scan) ([e9191f9](https://github.com/kiongahq/deploy/commit/e9191f9635f36cc054d2d0c342c960942a3c95be))
* pass pressure-test service names as a quoted array ([64c753c](https://github.com/kiongahq/deploy/commit/64c753c9f61580e69216970a6421021ef34a5782))


### Maintenance

* ignore local tooling and secret files ([8d8d8f8](https://github.com/kiongahq/deploy/commit/8d8d8f8e3e440ea6c2ca5a812350cf845ea2a780))
* license repository under MIT ([c46b865](https://github.com/kiongahq/deploy/commit/c46b8657e1c7e0d0053c8fdf3250511d008b057e))
* move to the kiongahq org; Go module github.com/kiongahq/platform, ghcr.io/kiongahq images, Kionga product name ([3c860d1](https://github.com/kiongahq/deploy/commit/3c860d1c755fb5555020685cd25560d2ac9e91de))
* remove the platform PRD document and all references to it ([abe9f78](https://github.com/kiongahq/deploy/commit/abe9f78427bf65f88e62eda0a671dba9ee4b5094))
* standalone deploy repo: compose builds from sibling component checkouts, workspace bootstrap, cross-repo and Helm executor tests, CI and nightly pressure ([492952d](https://github.com/kiongahq/deploy/commit/492952dce4dcfc3dc56094d934f44cc158426802))

## Changelog

Reviewed release pull requests add versioned changes here.
Existing tags and releases remain available in [GitHub](https://github.com/kiongahq/deploy/releases).
