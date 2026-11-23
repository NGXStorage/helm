CHARTS := nfs/kubernetes nfs/openshift fc/kubernetes fc/openshift iscsi/kubernetes iscsi/openshift
REPO_DIR ?= $(CURDIR)/repo
REPO_URL ?= https://ngxstorage.github.io/helm

.PHONY: lint template validate-rendered coexistence log-contract security-contract security-negative coordination-contract image-reference-contract published-image-contract package index pages check-pages test

lint:
	@set -e; for chart in $(CHARTS); do helm lint --strict "$$chart"; done

template:
	@set -e; for chart in $(CHARTS); do helm template ci "$$chart" >/dev/null; done

validate-rendered:
	./scripts/validate-rendered.sh

coexistence:
	./scripts/check-coexistence.sh

log-contract:
	./scripts/check-log-contract.sh

security-contract:
	./scripts/check-security-contract.sh

security-negative:
	./scripts/check-security-negative-fixtures.sh

coordination-contract:
	./scripts/check-coordination-contract.py

image-reference-contract:
	./scripts/check-image-reference-contract.py

published-image-contract:
	./scripts/check-published-image-contract.py

package:
	REPO_DIR="$(REPO_DIR)" REPO_URL="$(REPO_URL)" ./scripts/package-repo.sh

index: package
	helm repo index "$(REPO_DIR)" --url "$(REPO_URL)"

pages: index
	@set -e; for package in "$(REPO_DIR)"/*.tgz; do \
		target="docs/$$(basename "$$package")"; \
		if [ -e "$$target" ]; then cmp "$$package" "$$target"; else cp "$$package" "$$target"; fi; \
	done
	cp "$(REPO_DIR)/index.yaml" docs/index.yaml
	$(MAKE) check-pages

check-pages:
	python3 scripts/check-pages.py "$(REPO_DIR)" docs

test: lint template validate-rendered coexistence log-contract security-contract security-negative coordination-contract image-reference-contract
