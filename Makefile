.PHONY: help install run wait test all daily member-new member-export note-new download clean seed-check seed-sync seed-add

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| sort \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "\033[36m%-18s\033[0m %s\n", $$1, $$2}'

install: ## Install Python dependencies
	pip install feedparser

run: ## Run arXiv daily digest (fetch + filter + digest)
	python3 arxiv_digest/Arxiv_filter.py --wait

wait: ## Run with auto-retry on 429 rate-limit (5 min wait)
	python3 arxiv_digest/Arxiv_filter.py --wait

test: ## Run offline tests (fetch + scoring + sources + routing + catch-up + verify + vault seed)
	python3 arxiv_digest/test_fetch.py && python3 arxiv_digest/test_filter.py && python3 arxiv_digest/test_sources.py && python3 arxiv_digest/test_classify.py && python3 arxiv_digest/test_catchup.py && python3 arxiv_digest/test_verify.py && python3 sync/test_seed.py

all: ## Full run: filter + download PDFs + rename
	python3 arxiv_digest/Arxiv_filter.py && make download

daily: ## Fetch → verify → download (gated, recommended)
	./arxiv_digest/run_daily.sh


# --- Members ---

member-new: ## Scaffold a new member workspace. Usage: make member-new NAME=zhangsan
	@test -n "$(NAME)" || (echo "Usage: make member-new NAME=<name>"; exit 1)
	@test ! -d members/$(NAME) || (echo "members/$(NAME) already exists"; exit 1)
	cp -r members/_template members/$(NAME)
	@echo "Created members/$(NAME)/"

member-export: ## Export a member's personal content. Usage: make member-export NAME=zhangsan
	@test -n "$(NAME)" || (echo "Usage: make member-export NAME=<name>"; exit 1)
	./offboarding/extract.sh $(NAME)

# --- Paper notes ---

note-new: ## Create a new paper note from template. Usage: make note-new NAME=zhangsan FILE=作者-关键词
	@test -n "$(NAME)" || (echo "Usage: make note-new NAME=<name> FILE=<author-keyword>"; exit 1)
	@test -n "$(FILE)" || (echo "Usage: make note-new NAME=<name> FILE=<author-keyword>"; exit 1)
	cp paper-notes/template.md members/$(NAME)/paper-notes/2026/$(FILE).md
	@echo "Created members/$(NAME)/paper-notes/2026/$(FILE).md — go fill it in."

# --- Vault → topics (knowledge-base/topics) ---

seed-check: ## Report which vault notes changed since the last upload (read-only)
	python3 sync/seed.py

seed-sync: ## Re-upload drifted vault notes. Opts: make seed-sync ARGS="--apply"
	python3 sync/seed.py $(ARGS)

seed-add: ## Add/swap a category seed. Usage: make seed-add CAT=ocs SRC=OCS/Papers/MixNet_Analysis.md
	@test -n "$(CAT)" || (echo "Usage: make seed-add CAT=<cat> SRC=<vault-relative md>"; exit 1)
	@test -n "$(SRC)" || (echo "Usage: make seed-add CAT=<cat> SRC=<vault-relative md>"; exit 1)
	python3 sync/seed.py --add $(CAT) "$(SRC)" $(ARGS)

# --- Maintenance ---

download: ## Download PDFs + rename to Author_Year_Title. Opts: make download ARGS="--dry-run"
	python3 arxiv_digest/download_papers.py $(ARGS) && python3 arxiv_digest/rename_papers.py

clean: ## Remove generated files
	rm -f arxiv_digest/daily_digest.md
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null; true
