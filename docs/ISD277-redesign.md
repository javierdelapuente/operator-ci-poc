| Index | ISD277 |  |  |
| Title | operator-workflows redesign |  |  |

# Abstract

This specification introduces a new approach for the most taxing pipelines in the Platform Engineering group, mostly related to integration testing and publishing.

A redesign of the integration testing and publishing pipelines is proposed, that replaces the monolithic \`operator-workflows\` TypeScript/YAML/Bash approach with a modular, local-first model based on a build plan, a build output, and spread-based test execution.

# Rationale

The [operator-workflows](https://github.com/canonical/operator-workflows) repository is used by around 50 repositories in the Canonical organization. Thanks to the shared workflows, we ensure consistency, simplify maintenance and reduce boilerplate. Most of the shared workflows are straightforward with the main exception of the [integration\_test.yaml](https://github.com/canonical/operator-workflows/blob/main/.github/workflows/integration_test.yaml) workflow, the main concern of this specification.

[integration\_test.yaml](https://github.com/canonical/operator-workflows/blob/main/.github/workflows/integration_test.yaml) has served us well, as a shared workflow to build artifacts and run integration tests. However, it is showing its limitations:

- Lack of support for monorepos. It is not capable of associating charms and rocks and the publish workflow is not prepared for monorepos.  
- Scope too wide in integration tests. As of Mar 24, 2026 [integration\_test.yaml](https://github.com/canonical/operator-workflows/blob/main/.github/workflows/integration_test.yaml) has 50 input variables and lots of mixed concerns, like zap, load tests, trivy, provisioning, devstack installation… It has organically grown as a mixture of yaml/typescript/bash, which is hard to understand and maintain. Adding or removing extra functionality is prone to making many repositories fail.  
- It is not trivial to replicate what the workflow does in a local environment. Pipelines are not self documented.  
- Lack of pinning. This increases the blast radius of any change and increases the risk of security incidents like supply chain attacks.  
- High cognitive complexity. It is difficult to reason about and, subsequently, debug the behavior of the pipeline  
- Lack of extensibility. Scripts at certain points are starting to appear as “poor man's extension points”. Can’t be executed outside of the GitHub platform, causing platform lock-in and longer feedback development cycles.  
- Can’t be unit tested.

# Specification

## Scope of the specification

Redesign of the [integration\_test.yaml](https://github.com/canonical/operator-workflows/blob/main/.github/workflows/integration_test.yaml) and publish workflows. The rest of the workflows and actions are outside the scope of this specification. 

- Building the artifacts: charms, rocks and snaps.  
- Provision the environment to deploy the artifacts and run tests.  
- Running integration and end-to-end tests.  
- Publishing artifacts to the stores from a validated run.     


## Design objectives for operator-workflows redesign

- Modularity. The pipeline is composed of independent stages (build, test, publish…) that can be used separately or combined, and new stages can be added without modifying the existing ones.  
- Extensibility. Provide well-defined extension points so users can plug in custom behavior (e.g. custom provisioning, additional build steps) without the core pipeline growing without bounds.  
- Local testing. Make it straightforward to build artifacts, provision the environment and run tests locally.  
- Performance. The new design must not be slower than the current pipeline.  
- Debuggability. Make it easy to inspect and reproduce failing jobs.  
- Convention over configuration. Support diverse repository structures (monorepos, 12-factor charms, non-standard layouts) with sensible defaults  for standard ones.  
- Reproducibility. The full pipeline and its dependencies must be pinned.  
- Easy migration. Existing repositories should be able to adopt the new pipeline incrementally.

## Overview

### **How integration\_test.yaml works today**

Currently, the [integration\_test.yaml](https://github.com/canonical/operator-workflows/blob/main/.github/workflows/integration_test.yaml) follows a monolithic approach, but internally there are well differentiated jobs/steps. 

A very simplified summary of how integration\_test.yaml works is as follows:

-  The first part is the “[plan](https://github.com/canonical/operator-workflows/blob/e243dac0b57ff685d35033fe1003335168bd9910/.github/workflows/integration_test.yaml#L164)”. The plan looks for the artifacts (charms, rocks, docker and other resources) in the repository and generates a plan with the list of the artifacts and the output location for the build.   
- The “[build](https://github.com/canonical/operator-workflows/blob/e243dac0b57ff685d35033fe1003335168bd9910/.github/workflows/integration_test.yaml#L201)” jobs run in a matrix (one per artifact identified in the plan job).   
- The “[integration-test](https://github.com/canonical/operator-workflows/blob/e243dac0b57ff685d35033fe1003335168bd9910/.github/workflows/integration_test.yaml#L362)” job runs after the “plan” in a matrix for each module to test. It runs many steps, that can be somewhat grouped into:  
  - Provisioning the machine, tooling…   
    - There are some GitHub specific configurations. Configuring the credentials for the [ghcr.io](http://ghcr.io) registry and using a [docker.io](http://docker.io) mirror in self-hosted runners.  
  - Plan-provisioning step, that waits for the build jobs to finish, gets the artifacts and prepares the arguments for the integration tests with the list of artifacts.  
  - Run the integration test.

The integration\_test.yaml receives all its inputs through input variables. None of its parts can be run locally.

### **What changes in the new design**

The new design preserves the same three phases (plan, build, test) but makes  each one explicit and composable. Four key changes enable this.

**Explicit build plan (artifacts.yaml)**. The list of artifacts — charms, rocks  and the relations between them (e.g. which rocks are resources of which charms) — is declared in an artifacts.yaml  file optionally committed to the repository. This makes the build plan inspectable and editable, rather than  inferred at runtime. For standard repository layouts,  “opcli artifacts init”  can generate the file automatically.

**Build output as a stable interface (artifacts-generated.yaml).** After the  artifacts are built (locally or in CI), the build step produces an artifacts-generated.yaml  file that extends the build plan with the paths of the built artifacts. This file is the explicit input to the test  phase, and can also be consumed by other pipelines, like publishing, Trivy scanning or load testing, without coupling them to the integration-test workflow.

**Concierge for environment provisioning**. [Concierge](https://github.com/canonical/concierge/)  replaces the ad-hoc provisioning steps scattered across the current workflow. A  concierge.yaml  file in the repository declaratively describes the test environment (LXD, MicroK8s, Juju controller, etc.). Concierge serves a dual purpose: it is a simple provisioning tool that enables local testing, and it is the bridge between local and CI environments — differences such as image mirrors are expressed  as overrides to the same concierge file.

**Spread for test execution**. [Spread](https://github.com/canonical/spread)  orchestrates the full test run (provisioning \+ test execution) through a spread.yaml  file with two backends:

- Local ( local: ): provisions an LXD VM, runs concierge inside it, and uploads OCI images to a local registry.  
- CI ( ci: ): runs on the current machine, provisions with a CI-adapted concierge file, waits for artifacts to be built in parallel, downloads them,  and prepares them for the tests. 

After backend provisioning, spread tasks (or variants within a task) run the integration tests. For standard repositories,  spread.yaml  and  task.yaml  can be auto-generated with test auto-discovery. For more complex setups  (multiple Juju versions, different Kubernetes substrates, extra provisioning), the user customizes the spread files directly. Spread's prepare  and  prepare-each  hooks provide additional extension points beyond what concierge covers. 

A new CLI tool called  opcli  ties these pieces together. It generates  configuration files, builds artifacts, runs concierge, expands and runs  spread, and assembles pytest arguments — making it straightforward to execute each phase both locally and in CI with a single command.

## Integration-test new workflow

Both local and GitHub workflows follow the same sequence: build artifacts,  provision the substrate, run tests. Locally, the developer drives each step  from the command line. In CI, GitHub jobs run in parallel across a matrix of artifacts and test modules. The opcli commands are mostly the same in both cases — only the orchestration layer differs.

### **Local example**

Local testing using spread:

```shell
opcli artifacts init
opcli artifacts build
# creates spread.yaml and tests/run/task.yaml.
opcli spread init
# "opcli spread run" is a wrapper for spread that expands the backends and runs spread as a subprocess
opcli spread run -list
opcli spread run local:ubuntu-26.04:tests/run:test_charm
```

For quick local iteration, spread can be skipped, but extra provisioning in spread.yaml/task.yaml has to be done manually.

```shell
opcli artifacts init
opcli artifacts build

# Runs `sudo concierge prepare -c concierge.yaml`, puts image artifacts in the image registries...
opcli provision run
# If spread.yaml or task.yaml was modified manually, that prepare section should be executed here.
# Runs "tox -e integration -- --charm-file=... ..."
opcli pytest run -- -k test_charm
```

### **CI (GitHub)**

The CI workflow mirrors the local sequence but distributes work across  parallel GitHub jobs: 

1. **Plan**. Reads  artifacts.yaml  and produces a build matrix (one entry per  artifact).  
2. **Build (matrix).** Each job builds one artifact (charm, rock, etc.). A final  collection step waits for all build jobs to finish, assembles  artifacts- generated.yaml , and uploads it as a GitHub artifact.  
3. **Test plan**. Parses  spread.yaml  to discover the test tasks and produces a test matrix (one entry per test module).  
4. **Test (matrix).** Each job runs a single spread test with the  ci:  backend, which targets the current GitHub runner. Inside each job:  
   1. Backend prepare  
      1. opcli is installed  
      2. concierge.yaml  is patched with docker mirrors and any other CI specific overrides.  
      3. The machine is provisioned with concierge.  
      4. The job waits for the build collection step and downloads  artifacts-generated.yaml  
   2. The integration test is executed following a normal spread job.

## Contract for the repository and pipeline steps

The following files serve as the interfaces for the different steps of the pipeline. Some of them can reside in the repository.

| File | Required in repository | Purpose |
| :---- | :---- | :---- |
| artifacts.yaml | Optional. Can be autogenerated or created manually. | Declares the charms, rocks, images, snaps in the repository. It also links charms with its resources. |
| artifacts-generated.yaml | No. Generated by the build step. Should be in .gitignore. | Extends the artifacts.yaml with the paths or references of the build artifacts. |
| concierge.yaml | Yes. | Declarative provisioning \- juju, microk8s, ck8s, lxd, addons, constraints, host packages… |
| spread.yaml  | Optional. Can be autogenerated or created manually. | Spread configuration. The backend section is expanded by opcli to adapt to local/CI execution and provision the machine. It defines the suites for all the integration tests. |
| tests/run/task.yaml | Optional. Can be autogenerated or created manually. | Spread task with information on how to run an integration test. |

### **artifacts.yaml**

```
version: 1
rocks:
- name: indico
  source: indico_rock
- name: indico-nginx
  source: nginx_rock
charms:
- name: indico
  source: .
  resources:
    indico-image:
      type: oci-image
      rock: indico
    indico-nginx-image:
      type: oci-image
      rock: indico-nginx
```

### **artifacts-generated.yaml**

**Locally** — always local files:

```
version: 1
rocks:
  - name: indico
    source: indico_rock
    output:
      file: ./indico_rock/indico_1.0_amd64.rock
charms:
  - name: indico
    source: .
    output:
      file: ./indico_ubuntu-22.04-amd64.charm
```

**In CI** — artifacts in GitHub or OCI images pushed to GHCR:

```
version: 1
rocks:
  - name: indico
    source: indico_rock
    output:
      image: ghcr.io/canonical/indico:abc1234-22.04
charms:
  - name: indico
    source: .
    output:
      artifact: charm-indico
      run-id: 1234567890
```

### **concierge.yaml**

Standard [concierge](https://github.com/canonical/concierge) configuration file. In CI, the pipeline patches this file in place before running concierge to inject GitHub-specific settings (Docker/GHCR mirror credentials, registry configuration). See [canonical/concierge\#181](https://github.com/canonical/concierge/issues/181) for the current identified limitation.

One example is the k8s preset:

```
juju:
  model-defaults:
    test-mode: "true"
    automatically-retry-hooks: "false"

providers:
  lxd:
    enable: true
  k8s:
    enable: true
    bootstrap: true
    bootstrap-constraints:
      root-disk: "2G"
    features:
      load-balancer:
        l2-mode: "true"
        cidrs: "10.43.45.0/28"
      local-storage: {}
      network: {}

host:
  packages:
    - gnome-keyring
    - python3-pip
    - python3-venv
  snaps:
    charmcraft:
    jq:
    yq:
    rockcraft:
```

### **spread.yaml**

The  spread.yaml  file defines a mandatory backend called  integration-test  — a virtual backend that  opcli  expands into the real  local:  or  ci:  backend  before running spread. This file can be auto-generated with “opcli spread init”  (which discovers all integration tests in the repository) and then optionally edited and committed. Similar to  “charmcraft test”  and  “snapcraft test”, the backend expansion happens transparently using “opcli spread run”.

```
project: indico-operator

backends:
  # Virtual backend. Expanded by opcli into local: or ci:
  integration-test:
    systems:
      - ubuntu-24.04:
          runner: [self-hosted, noble]

environment:
  # Different variants can be added if different concierge files are needed, 
  # for example for Juju 3 and Juju 4, or MicroK8s and CK8s. Combined with variants,
  # tests can be run with different concierge files.
  CONCIERGE: '$(HOST: echo "${CONCIERGE:-concierge.yaml}")'
  CONCIERGE/test_charm_juju4: concierge_juju4.yaml

suites:
  tests/:
    environment:
      MODULE/test_charm: test_charm
      MODULE/test_charm_juju4: test_charm
      MODULE/test_actions: test_actions
    prepare: |
      # optional: user-defined steps, run after provisioning is complete
```

“opcli spread expand “ can be used to preview the fully expanded file without running it.

Users can customize the full  spread.yaml  (adding backends, suites, environment variables, etc.), but if the changes diverge significantly from the conventions, bypassing spread with  “opcli provision run”  \+  “opcli pytest run”  may no longer replicate the same behavior.

### **Task.yaml**

tests/run/task.yaml

By default this file will be placed under tests/run and auto-generated by “opcli spread init”. 

```
summary: integration tests

environment:
  # MODULE variants could be there instead of spread.yaml

execute: |
    $( opcli pytest run -- -k $MODULE )
```

The  MODULE  environment variable is set by the spread variant (defined in  spread.yaml ). Each variant runs one test module.

## Tooling (opcli)

A CLI tool called  opcli  facilitates auto-generation of configuration files, building artifacts, provisioning and running tests. It abstracts the differences between local and CI execution.

The functionality for opcli is grouped into four command families:  artifacts , provision ,  spread  and  pytest . Additional commands and subcommands can  be added as needed.

### **opcli artifacts**

| Command | Description | Extra options |
| :---- | :---- | :---- |
| opcli artifacts init | Discovers charms, rocks and snaps in the repository and generates artifacts.yaml |  |
| opcli artifacts build | Builds all artifacts declared in artifacts.yaml and produces artifacts-generated.yaml If charms or rocks arg is used, only those artifacts will be built. |  —charm \<charm-name\>, charms to build. Can be used several times. —rock \<rock-name\>, rocks to build Can be used several times.  |

### **opcli provision**

| Command | Description | Extra options |
| :---- | :---- | :---- |
| opcli provision run | Runs concierge prepare to provision the test. |  |
| opcli provision load | Loads the image artifacts into the local image registry. | \-r registry. Specify the image registry. Defaults to localhost:32000 |

### **opcli spread**

| Command | Description | Extra options |
| :---- | :---- | :---- |
| opcli spread init | Discovers integration tests and generates spread.yaml and tests/run/task.yaml |  |
| opcli spread run | Expands the virtual backend in spread.yaml and runs spread as a subprocess, passing the same arguments as received. | As in craft-application, the CI env var defines whether to run locally or in the pipeline. All arguments are passed to the spread subprocess. |
| opcli spread expand | Prints the fully expanded spread.yaml | As in craft-application, the CI env var defines whether to run locally or in the pipeline |

### **opcli pytest**

| Command | Description | Extra options |
| :---- | :---- | :---- |
| opcli pytest run | Runs integration tests with tox with the arguments as in “opcli pytest args” | \-e tox environment. By default “integration”.  “- \-” passes extra arguments to pytest |
| opcli pytest args | Prints the assembled tox/pytest flags to stdout without running it, using as input the file artifacts-generated.yaml. Useful for debugging/manual script invocation. Follows the same conventions as currently in operator-workflows. |  |

## Design decisions

### **opcli**

 There are several alternatives for the implementation language:

- TypeScript/npm. Native option for GitHub Actions. However it introduces a language not widely used in the team, and for pinning using the Github “use:”,  it creates the requirement of having two repositories, one for the actions and one for the workflows. More risk of eventual platform lock-in.  
-  Python. The team is familiar with Python, although the integration with GitHub is not so elaborated. Pinning to the current workflow commit SHA can be done with tools like canonical/get-workflow-version-action.   
- Go. Similar situation as with Python, but the team is not so familiar with Go.

The decision proposed for this specification is Python.

### **concierge**

Concierge is a purpose-built tool for charm test environments (Juju, LXD, MicroK8s, CK8s) in a single declarative file. The same  concierge.yaml  works locally and in CI — differences like mirrors are expressed as patches. It is maintained within Canonical with active development.

Its main limitation for the purpose of this specification is that there are functionalities not implemented that are required and will have to be done (see [https://github.com/canonical/concierge/issues/181](https://github.com/canonical/concierge/issues/181)) and repositories with unusual provisioning requirements fall outside of this tool. For those cases, the solution is to use spread.yaml prepare steps.

### **spread**

The current pipeline has no way to reproduce a test job locally. Spread is a Canonical developed tool, described as a “Convenient full-system test (task) distribution”, that among other things, allows running jobs (tests) in different environments.

Spread solves some of the problems in this specification:

- The tests and how they are executed are defined in spread configuration files.  
- With the help of two different backends, tests can be run in ci (in the current machine) or locally (in a lxd virtual machine).  
- Extra provisioning not supported by concierge can be added to spread configuration files.

Besides that, with a use of a “virtual backend” that is not spread syntax, but expanded by the opcli tool:

- We can define the main provisioning and isolate the differences between running tasks in the GitHub and locally. Depending on how the virtual backend “integration-test” is expanded, it will provision and get/use the artifacts in a different way.

As disadvantages of using spread:

- It is a new tool that adds complexity. Using and debugging it requires familiarity with it.  
- Putting extra provisioning in spread configuration adds drifting between repositories. The alternative considered was a bash file, which has similar issues.

Spread is gaining adoption in Canonical, and is already used in a similar way by tools like “charmcraft test” and “snapcraft test”

## Alignment of the proposed design with the design objectives

| Objective | How the design addresses it |
| :---- | :---- |
| Modularity | Each phase (build, provision, test, publish) is an independent stage with file-based interfaces ( artifacts.yaml  →  artifacts-generated.yaml  → spread). Stages can be used separately or composed. |
| Extensibility | Concierge presets, spread  prepare / prepare-each  hooks, the  CONCIERGE  variant, and the stable  artifacts-generated.yaml  interface allow users to extend behavior at well-defined points. |
| Local testing | Every  opcli  command runs locally.  “opcli spread run“ and  “opcli provision run”  \+  “opcli pytest run” give two paths to run the full test cycle on a developer machine. |
| Performance | All of the current optimizations In GitHub can be run with the new design (matrix build of artifacts, matrix run of tests, running provisioning in parallel to the building of artifacts…) |
| Debuggability | Developers can reproduce any CI job locally with the same  opcli  commands. Intermediate files ( artifacts.yaml ,  artifacts-generated.yaml ,  concierge.yaml ) are inspectable.  “opcli spread expand”  previews the full spread configuration. |
| Convention over configuration | Standard layouts work with auto-generated files ( opcli artifacts init ,  opcli spread init ). Non-standard layouts override only the files that differ. |
| Reproducibility | Mostly unrelated to the specification, in GitHub workflows, all should be pinned. The opcli tool could be installed from the same repository as the workflows and use the same commit hash. |
| Easy migration | A new integration\_test.yaml workflow can be created combining the new tooling in a similar way as the old one. With a few changes in the repositories (being the only mandatory one the  concierge.yaml file) the migration can be done. This step can probably be done automatically by AI. |

# Further Information

## Spread for testing tutorials

Spread has been added in some Platform Engineering repositories to test the tutorials. A spread.yaml file is added and the task.yaml is autogenerated based on the content of the tutorials (written in Markdown or reStructuredText).

In a similar fashion as the integration tests in this specification, a backend that can be expanded differently locally and in GitHub can be created. A new opcli command can be created that will handle the executable expansion of the tutorials.

For example, this could be achieved with a spread.yaml that could also be run with “opcli spread run” like:

```
project: indico-operator

backends:
  tutorial-test:
    # Not a real spread backend. It will be expanded by opcli
    systems:
      - ubuntu-24.04:
          runner: [self-hosted, noble]

suites:
  tests/tutorial/:
    environment:
      TUTORIAL/tutorial1: docs/tutorial1.rst
      TUTORIAL/tutorial2: docs/tutorial2.rst
```

And a “tests/tutorial/run/task.yaml” like:

```
summary: tutorial test

execute: |
    $(opcli tutorial expand $TUTORIAL)
```

See [https://github.com/canonical/haproxy-operator/blob/main/spread.yaml](https://github.com/canonical/haproxy-operator/blob/main/spread.yaml), [https://github.com/canonical/operator-workflows/blob/main/.github/workflows/docs\_spread.yaml](https://github.com/canonical/operator-workflows/blob/main/.github/workflows/docs_spread.yaml) and [https://github.com/canonical/operator-workflows/blob/main/spread/create\_spread\_task\_file.py](https://github.com/canonical/operator-workflows/blob/main/spread/create_spread_task_file.py).

## Pre-run scripts inventory

| Repo | What it does |
| :---- | :---- |
| haproxy-operator | LXD controller; \`juju add-k8s\` |
| github-runner-image-builder-operator | charmcraft pack, nft config for juju \- PROBLEMATIC |
| synapse-operator | Localstack |
| postfix-relay-operators | Mailcatcher (docker), extra juju controller |
| postfix-relay-operators | Bootstrapping extra controller in juju (and microk8s configuration). |
| smtp-relay-operator | Run Mailcatcher (with docker) |
| github-runner-operator | Extra juju controller (and microk8s configuration). |
| discourse-k8s-operator | S3 installation (microceph) |
| wordpress-k8s-operator | Extra controller bootstrapping in microk8s. |
| hockeypuck-k8s-operator | Metallb plugin in microk8s |
| flask-multipass-saml-groups | Install several apt packages and calls pyenv commands |
| wazuh-server-operator | Bootstrap lxd controller and sets several sysctl parameters |
| opencti-operator | Bootstraps lxd controller, sets several sysctl parameters deletes files and installs a manifest in microk8s. For sysctl we should probably use sysconfig charm. |
| smtp-dkim-signing-operator | Installs apt package and runs mailcatcher with docker |
| opendkim-operator | Builds snap, installs snap, Installs apt package and runs mailcatcher with docker |
| jenkins-agent-operator | Configures microk8s, bootstraps microk8s controller |
| jenkins-agent-k8s-operator | Configures microk8s, bootstraps lxd controller |
| mattermost-k8s-operator | Localstack |
| penpot-operator | Runs: tox \-e playwright-install |
| tmate-ssh-server-operator | Creates directories and runs ssh-keygen |
|  |  |

## Related specifications

[OP084 \- Standardised Charm Dev Workflows](https://docs.google.com/document/d/1A3x0sWfQbDc7njiBuyFpcy4mpcIlyXCSf9ru-Fz_WL0/edit?tab=t.0)   
[OP061 \- Linting and Testing Command Standardisation in Charms](https://docs.google.com/document/d/1GfOTT1Ir-pLAbILUrI4GS9T8AAI8Ni8gpF1Mh67Wx3E/edit?tab=t.0)

## Related issues

[ISD-4882: \[Operator workflows\] Update publish charm to support multiple plan outputs from parametrized integration tests](https://warthogs.atlassian.net/browse/ISD-4882)

## Related projects

### **Observability pipeline**

[https://docs.google.com/document/d/1beNGXcGtRWZYWz4ssZJjm6fwa-xF5LCjjqMX7RqIfsw/edit?tab=t.0\#heading=h.gltgdcz5tfwd](https://docs.google.com/document/d/1beNGXcGtRWZYWz4ssZJjm6fwa-xF5LCjjqMX7RqIfsw/edit?tab=t.0#heading=h.gltgdcz5tfwd)

### **Data platform pipelines**

[https://docs.google.com/document/d/1A6xAvC1Cv3IRXFK6hhL5\_4K8faQj68ZJO9cKszsXQN4/edit?tab=t.0\#heading=h.kck5sj517pqk](https://docs.google.com/document/d/1A6xAvC1Cv3IRXFK6hhL5_4K8faQj68ZJO9cKszsXQN4/edit?tab=t.0#heading=h.kck5sj517pqk)

### **Identity pipelines**

[https://github.com/canonical/identity-team/](https://github.com/canonical/identity-team/)  
[https://github.com/canonical/identity-credentials-workflows/tree/v0/.github/workflows](https://github.com/canonical/identity-credentials-workflows/tree/v0/.github/workflows)
