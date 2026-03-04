# CLAUDE.md

This file provides guidance for AI assistants working with this repository.

## Repository Overview

- **Name**: anthropic
- **Owner**: kuchak
- **Status**: New project (initial setup)

## Project Structure

This repository is in its initial state. As the project grows, document the directory layout here:

```
/
├── CLAUDE.md          # AI assistant guidance (this file)
├── planning/          # Plans, strategies, and roadmaps (.md files)
└── (project files)    # To be added
```

## Development Setup

### Prerequisites

<!-- Update with actual requirements as the project develops -->
- Git

### Getting Started

```bash
git clone <repository-url>
cd anthropic
# Add setup steps as project develops (e.g., dependency installation)
```

## Build & Run

<!-- Update these sections as build tooling is added -->

| Task    | Command |
| ------- | ------- |
| Build   | TBD     |
| Test    | TBD     |
| Lint    | TBD     |
| Format  | TBD     |

## Testing

<!-- Document testing patterns once a test framework is adopted -->
- Test framework: TBD
- Test location: TBD
- Run all tests: TBD
- Run a single test: TBD

## Code Conventions

<!-- Update as the team establishes conventions -->
- Follow consistent formatting (configure a formatter once the language/framework is chosen)
- Write clear commit messages describing the "why" not just the "what"
- Keep changes focused — one logical change per commit

## Architecture

<!-- Document key architectural decisions and patterns as they are made -->

No architecture decisions have been recorded yet. Update this section as the project takes shape.

## Key Files

<!-- List important files and their purposes as they are created -->

| File        | Purpose                                  |
| ----------- | ---------------------------------------- |
| CLAUDE.md   | AI assistant guidance                    |
| planning/   | Plans, strategies, and roadmap documents |

## Planning & Strategy Documents

All planning documents, strategy write-ups, and architectural plans live in the `./planning/` folder as Markdown files.

- When the user asks for a plan, strategy, roadmap, or any forward-looking document, save it as a `.md` file in `./planning/`
- Use descriptive filenames with kebab-case (e.g., `api-migration-plan.md`, `q2-growth-strategy.md`)
- If the `./planning/` folder doesn't exist yet, create it before writing the document

## Notes for AI Assistants

- This is a new repository — verify what files exist before assuming project structure
- When adding new tooling or frameworks, update this CLAUDE.md with relevant commands and conventions
- Always read existing code before proposing modifications
- Prefer minimal, focused changes over large refactors
