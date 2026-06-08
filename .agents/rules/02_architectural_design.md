# 📐 Rule 02: Architectural Design Standards

## 1. Goal
To ensure all architectural modifications follow clean design principles, domain boundaries, and solid OOP/functional practices, avoiding code spaghetti and tightly coupled logic.

## 2. Core Architectural Pillars
- **Clean Architecture & Separation of Concerns**: Keep business/domain logic isolated from transport protocols (HTTP/gRPC/CLI), storage/databases, and external integrations.
- **SOLID Design Principles**:
  - *Single Responsibility*: A class/module should have only one reason to change.
  - *Open/Closed*: Software entities should be open for extension but closed for modification.
  - *Liskov Substitution*: Subtypes must be substitutable for their base types.
  - *Interface Segregation*: Prefer small, focused interfaces over large, monolithic ones.
  - *Dependency Inversion*: Depend on abstractions, not concretions.
- **Domain-Driven Design (DDD)**: Model software around a bounded context. Maintain clear aggregate roots, value objects, entities, and services.

## 3. Contract-First Development
- **APIs**: Always define API interfaces (e.g. OpenAPI, Swagger, Protocol Buffers, GraphQL schema) *before* writing handler code.
- **Interfaces**: Define programming interfaces or types first. Ensure callers depend on interfaces rather than implementation details.
- **Mockability**: Architect components so they can be easily mocked or stubbed for testing.
