# Prism 0.18.0 language briefing

This is general language/toolchain guidance, not a solution to the task. The
published problem and the existing starter define the required behavior. You have
Prism 0.18.0, its native toolchain, and offline documentation. Do not assume a
Python, TypeScript, Rust, Haskell, or newer Prism API exists with the same spelling.

## Syntax and state

Prism uses indentation for blocks. Functions are `fn name(arg : Type) : Type = ...`;
exported declarations use `pub`. Calls use `f(a, b)`; lambdas use `\(a, b) -> expr`.
Branches are `if condition then value elif condition then value else value`.
Pattern matching is `match value of` with indented `Pattern => expression` arms.
A function/block returns its last expression. Use `--` for line comments. Local
bindings use `let name = expression`; 0.18.0 does not accept a local
`let name : Type = expression`. Put needed annotations on function parameters
and results.

Type application is `List(Int)`, `Option(String)`, `Result(Int, String)`, and
`Map(String, Int)`. `Option` constructors are `Some(value)` and `None`; `Result`
constructors are `Ok(value)` and `Err(error)`. An error-propagating `value?` belongs
in a function returning the corresponding result type. Do not invent exceptions
or nullable host-language values for these types.

Records name a constructor. Field access is `state.count`; an immutable update is
`Counter { ..state, count = new_count }`. `let` binds an immutable value. Returning
an updated record/map does not mutate an older binding or snapshot. Sum types use
`type Status = Ready | Done(Int)`; `deriving (Eq)` adds equality when its fields
support it. Keep existing module boundaries and public interfaces coherent.

This complete program illustrates records, results, matching, and an update:

```prism
import Data.Result (..)

type Counter = Counter { count: Int }

fn nonnegative(n : Int) : Result(Int, String) =
  if n < 0 then Err("negative") else Ok(n)

fn add(c : Counter, amount : Int) : Result(Counter, String) =
  let checked = nonnegative(amount)?
  Ok(Counter { ..c, count = c.count + checked })

fn main() =
  let original = Counter { count = 2 }
  match add(original, 3) of
    Ok(updated) => println(show_int(updated.count))
    Err(message) => println(message)
  println(show_int(original.count))
```

Its output is `5`, then `2`, on separate lines.

## Collections and their costs

`List(a)` is a singly linked list: `Nil` or `Cons(value, rest)`, also written
`[a, b, c]`. `Cons` prepends in constant time; `nth(i, xs)` walks to index `i`.
Repeated indexed access is not array access. `length(xs)` traverses a list.
`append(xs, ys)` traverses the first list; repeatedly appending one item to a
growing list can be quadratic. When appropriate, accumulate by prepending and
reverse once, or consume the list with a fold/pattern match. Preserve required
ordering explicitly when changing representation.

`Data.Map` provides a persistent AVL ordered map, with logarithmic-height lookup,
insertion and deletion (plus key-comparison costs). Updates return a new map and
can share unchanged structure. `map_size` traverses the tree in this version; it
is not a cached constant-time length. Scanning an association list or converting
an entire map to a list on every lookup discards the benefit of the map.

Common signatures/argument order:

- `map(f, xs)`, `filter(predicate, xs)`, `foldl(f, initial, xs)`; a fold callback
  receives `(accumulator, element)`.
- `nth(index, xs)` returns an `Option`; `reverse(xs)` reverses a list.
- `map_empty` is a value, not a function call.
- `map_lookup(key, m)` returns `Option(value)`.
- `map_insert(key, value, m)` and `map_delete(key, m)` return updated maps.
- `map_result(f, result)` transforms success; `map_err(f, result)` transforms error.

Map operations require an ordering for their keys. Prefer existing supported key
types or an explicitly derived ordering. A tuple or nested maps preserve multiple
key components; concatenating strings needs an unambiguous encoding.

```prism
import Data.Map (..)
import Data.List (..)

fn initial_counts() : Map(String, Int) = map_insert("blue", 3, map_empty)

fn main() =
  let counts = initial_counts()
  let more = map_insert("blue", 4, counts)
  match map_lookup("blue", more) of
    Some(n) => println(show_int(n))
    None => println("missing")
  println(show_int(foldl(\(acc, x) -> acc + x, 0, [1, 2, 3])))
```

Its output is `4`, then `6`. Language-level immutability does not make an algorithm
incremental: inspect what each operation traverses, rebuilds, or retains.

## Finding APIs and validating changes

Import local modules with `import Module (name)` or `import Module (..)`, matching
the starter. `Data.List` contains list-specific operations; `Data.Foldable`
contains generic queries such as `length`, `find`, `all`, and `any`. For JSON,
follow the starter's `Json` adapter and keep typed internal state where useful.

Look up exact APIs in `/opt/prism-lib/std/` and `/opt/prism-docs/stdlib/`; the language
reference is `/opt/prism-docs/spec.md`. For example, inspect `Data/Map.pr`,
`Data/List.pr`, `Data/Foldable.pr`, or `Data/Result.pr` before guessing a name.

Use `execute` for every task file operation; its filesystem is separate from the
native client's. Make a small edit and compile before expanding it. Run
`./starter/build.sh` from `/work` after source changes, then the task's published
`python3 run.py run ...` command. The launcher runs an already-built executable:
editing source without rebuilding can accidentally test stale code. Use small,
separate scratch programs to check unfamiliar syntax/APIs; do not replace the
starter's build/launch contract. Keep stdout reserved for the requested protocol.
