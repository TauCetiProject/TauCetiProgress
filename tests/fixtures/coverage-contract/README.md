# Roadmap: elliptic curves over finite fields

A small roadmap, invented for the producer/consumer contract test. Its shape is the common one:
a "what Mathlib has" section, then the build in worded `Layer` headings, then worked examples
whose headings must not be mistaken for layers.

## What Mathlib already has

- `WeierstrassCurve` and its group law on points.

## The build, in layers

### Layer 0: the group law (Silverman III.2)

The group structure on the rational points, and the coordinate formulas.

### Layer 1: isogenies and the dual — degree, separability

Isogenies as group homomorphisms, the dual isogeny, and `deg` as a multiplicative function.

### Layer 2: Hasse's bound

The bound `|#E(F_q) - q - 1| <= 2 sqrt q` from the degree of Frobenius minus one.

## Worked examples

### Example A: y^2 = x^3 + 1 over F_7

Six points.
