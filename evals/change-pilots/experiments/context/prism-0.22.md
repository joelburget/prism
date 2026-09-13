# Prism 0.22.0 language briefing and tutorial

This is general language/toolchain guidance, not a solution to the task. The
published problem and the existing starter define the required behavior. You have
Prism 0.22.0, its native toolchain, and offline documentation. Do not assume a
Python, TypeScript, Rust, Haskell, or newer Prism API exists with the same spelling.

This briefing includes a practical quick reference followed by the complete
upstream **Taste the Rainbow** tutorial and its eight chapters. Read both as
language onboarding: functions and values; data and patterns; effect types;
handlers and continuations; coeffects; lenses and streams; projects and content
identity; and the concluding programming habits.

The evaluation environment is already installed and offline. Tutorial installation,
Playground, package initialization, and Git setup commands are background examples,
not steps to perform for this task. Extend the supplied starter using its existing
build and JSON protocol. `execute` is the sole task filesystem/tool boundary.
The tutorial's phrase "no ordinary return" teaches expression-oriented style;
0.22 also supports explicit early `return` and local `var` as described below.
Examples marked `compile_fail` intentionally fail; `ignore` denotes a fragment that
needs the surrounding project. Hidden book scaffolding is expanded here so the
standalone examples can be compiled as shown.

## Syntax and state

Prism uses indentation for blocks. Functions are `fn name(arg : Type) : Type = ...`;
exported declarations use `pub`. Calls use `f(a, b)`; lambdas use `\(a, b) -> expr`.
Branches are `if condition then value elif condition then value else value`.
Pattern matching is `match value of` with indented `Pattern => expression` arms.
A function/block returns its last expression. Use `--` for line comments. Local
bindings use `let name = expression`; 0.22.0 does not accept a local
`let name : Type = expression`. Put needed annotations on function parameters
and results, or use a typed `var` when you need a mutable local cell.

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

```output
5
2
```

A mutable local uses `var name := expression`, optionally with a type annotation:
`var name : Type := expression`. Reassignment is `name := expression`; compound
assignment includes `+=`, `-=`, `*=`, and `%=`. In 0.22, a field/index path rooted
at a `var` also accepts assignment, such as `state.count += 1`. A path rooted at a
`let` does not. These are functional updates with value semantics: previously
saved values remain unchanged. Updates can reuse uniquely owned storage; sharing
can require copying. Mutation syntax alone does not guarantee constant-time work.

`while condition do` and `for x in xs do` introduce indented loop bodies. `break`
and `continue` apply to loops; `return value` exits the enclosing function. For
example:

```prism
type Counter = Counter { count: Int }

fn main() =
  var state : Counter := Counter { count = 2 }
  let original = state
  var i := 0
  while i < 3 do
    state.count += i
    i += 1
  println(show_int(state.count))
  println(show_int(original.count))
```

```output
5
2
```

## Effects are part of the interface

A function type describes both the value returned and the effects that can escape:
`fn report(n : Int) : Unit ! {IO} = println(n)`. Omitting an explicit effect row
allows inference; it does not suppress IO or other effects. Effects from callees
propagate to the caller unless a local handler discharges them. `! {Ask, IO}` is a
closed row; `! {| e}` preserves an unknown row in an effect-polymorphic wrapper.
Do not erase a callback's effects by annotating it as a pure function.

Declare capabilities with `effect`, call their typed operations directly, and
interpret them with `handle ... with`. In `ask() resume k => k(answer)`, `k` is the
remaining computation up to the handler. The `return value => ...` clause handles
normal completion and can change the handler's result type. Operation grades
`never`, `once`, and `many` constrain resumption; `once` requires exactly one
resumption in tail position. These differ from a function-value usage coeffect
`@ once`, which permits at most one consumption. The tutorial below develops all
of these with complete executable examples, including row polymorphism and search.

Typed `error` / `throw` / `try` / `catch` also participate in effect rows. They are
available when appropriate; they do not turn an `Option` or `Result` into a
nullable host-language value. Preserve an existing interface's chosen error
representation. `var` is scoped state expressed through effects; locally handled
state can leave a pure outward signature. Effect handlers do not themselves
provide persistent transactions, process recovery, or external IO rollback.

Coeffects use `@` to express checked usage/resource contracts, such as `@ noalloc`,
`@ bounded_stack`, and `@ once`. They are not effects and require no handlers.
Purity does not imply allocation-free execution or cheap collection operations.
Treat certificates as claims to justify with the compiler, not performance switches.

## Collections and their costs

`List(a)` is a singly linked list: `Nil` or `Cons(value, rest)`, also written
`[a, b, c]`. `Cons` prepends in constant time; `nth(i, xs)` walks to index `i`.
Repeated indexed access is not array access. `length(xs)` traverses a list.
`append(xs, ys)` traverses the first list; repeatedly appending one item to a
growing list can be quadratic. When appropriate, accumulate by prepending and
reverse once, or consume the list with a fold/pattern match. Preserve required
ordering explicitly when changing representation. For a list emptiness check,
use `Data.List.is_nil` or pattern matching, both constant time. Generic
`Data.Foldable.is_empty`, `find`, `all`, and `any` use strict folds and still
traverse the container even after the answer is known.

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
key components; concatenating strings needs an unambiguous encoding. The full
map type now carries an ordering brand, `Map(k, v, ord)`, so maps built with
different ordering witnesses cannot be accidentally combined. The two-argument
`Map(k, v)` spelling remains supported: the compiler infers the brand. For custom
comparators, read `Data.Ordered` and use its ordering-witness API rather than
inventing a comparator argument for `map_insert`.

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

```output
4
6
```

Language-level immutability does not make an algorithm
incremental: inspect what each operation traverses, rebuilds, or retains.

For dense numeric storage, `Data.FlatArray` supports `Float` and `I64` elements:
`fa_new(n, value)` is O(n), `fa_get(array, index)` and `fa_len(array)` are O(1),
and `fa_set(array, index, value)` is O(1) when uniquely owned but O(n) when a shared
buffer must be copied. It is not a general-purpose array of records.
`Data.IntMap` is a persistent trie for `I64` keys with traversal depth at most 64;
it does not accept arbitrary-precision `Int` keys. `to_i64` wraps to the low 64
bits, so conversion can change key identity. Choose representations that preserve
the problem's numeric and snapshot semantics.

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

## Complete upstream tutorial

The following tutorial is frozen from the same upstream 0.22 commit as the runtime.
Its original Markdown is also available at `/opt/prism-docs/tutorial.md` and
`/opt/prism-docs/tutorial/`. The compiler reference is `/opt/prism-docs/compiler.md`.
The final chapter uses satire; use the effect-types and coeffects chapters and
language specification for the technical meaning of those terms.

<!-- BEGIN PINNED UPSTREAM TUTORIAL -->

<!-- Source: docs/src/tutorial.md -->

# Taste the Rainbow

This is Prism for Python programmers. It assumes you know variables, functions, lists, and `if`, but it assumes no functional-programming experience. By the end you will be able to read and write a small multi-file Prism program, model data, transform collections, follow inferred types and effects, write a test, and understand the ideas that make Prism unusual.

The tutorial is a bridge, not a compressed language specification. Every new idea starts from a Python habit, replaces it with a Prism mental model, and ends with something you can run or deliberately break.

## What changes when you leave Python?

The punctuation is the easy part. These are the larger shifts:

| Python instinct                                      | Prism model                                               |
| ---------------------------------------------------- | --------------------------------------------------------- |
| execute statements and `return`                      | evaluate expressions to values                            |
| reassign names freely                                | bind immutable values with `let`                          |
| represent alternatives with classes or tags          | define the exact alternatives with an algebraic data type |
| use `match` as convenient control flow               | let exhaustive patterns prove every case is covered       |
| discover IO, mutation, and exceptions from the body  | read observable effects from the function type            |
| document “call once” or “does not allocate” in prose | express usage and resource promises as coeffects          |
| build nested copies by hand                          | focus and update immutable data with optic paths          |
| use iterators to avoid intermediate lists            | fuse stream producers, transformations, and consumers     |
| identify code by filenames and source hashes         | identify canonical Core definitions by content            |

Prism is **strict**: arguments are evaluated before a function runs, as in Python. It is also deliberately **impure**: useful programs print, read files, fail, and communicate. The difference is accountability. Prism infers those observations as named effects and lets handlers interpret them at explicit boundaries.

## How to use this tutorial

The quickest route is the browser-based [Playground](https://sdiehl.github.io/prism/play/). It needs no installation and is ideal for the single-file examples in the first five chapters.

For the project, module, test, and content-identity sections, use a local compiler. Prebuilt Prism supports Apple Silicon macOS and glibc Linux on x86-64 or AArch64. Native code generation needs LLVM 22:

```shell
# macOS
brew install llvm@22

# Debian or Ubuntu
curl -fsSL https://apt.llvm.org/llvm.sh | sudo bash -s 22
```

Install the compiler and confirm it is available:

```shell
curl --proto '=https' --tlsv1.2 -fsSL https://sdiehl.github.io/prism/install.sh | sh
prism --version
```

Homebrew users may instead run `brew install sdiehl/prism/prism`. The repository [README](https://github.com/sdiehl/prism#install) covers Nix, containers, Linux packages, and building from source.

Code marked **output** is what the preceding program prints. A **Try it** prompt is small enough to do immediately. Making the change matters more than merely reading the answer. All ordinary Prism blocks in this book are checked by the compiler, and intentionally broken blocks are checked to ensure they really do fail.

## Run your first program

Put this in the Playground or save it as `rainbow.pr`:

```prism
fn main() =
  let name = "Python programmer"
  println("Welcome, {name}. Taste the Rainbow!")
```

```output
Welcome, Python programmer. Taste the Rainbow!
```

Run a local file through the interpreter:

```shell
prism check rainbow.pr
prism run rainbow.pr
prism fmt rainbow.pr
```

That three-command loop is the ordinary workflow:

- `check` finds problems without executing the program.
- `run` interprets it immediately.
- `fmt` gives the source its canonical layout.

A program begins at `main`. A `let` binds a value without making a mutable variable. There is no ordinary `return`: the last expression is the value of the body. Indentation forms the body, comments start with `--`, and `{name}` inside a string interpolates an expression.

Most type annotations are optional. The compiler inferred that `name` is a `String`, `println` produces `Unit`, and `main` performs console IO. Hover those expressions in the rendered book to see the inferred facts.

## Let the compiler teach you

Python type annotations are usually advice to a separate checker. A Prism annotation is part of the program and the compiler must prove the body agrees with it:

```prism,compile_fail
fn square(n : Int) : Int = n * n

fn main() = println(square("six"))
```

This program is supposed to fail: `square` requires an `Int`, but the call supplies a `String`. Prism diagnostics carry stable codes. When an unfamiliar one appears, ask for its explanation:

```shell
prism explain E1002
```

Use the code printed by your own diagnostic. `E1002` is simply an example. The explanation includes the cause, a minimal reproducer, and a fix.

> **Try it:** Change the greeting to accept an `Int` named `count`, interpolate it into the message, then deliberately pass a string. Read the error before repairing the call.

## The route ahead

The chapters build on one another:

1. [Functions and Values](/opt/prism-docs/tutorial/functions.md) replaces statements and reassignment with expressions, immutable data flow, and higher-order functions.
2. [Data and Patterns](/opt/prism-docs/tutorial/data.md) introduces records, algebraic data types, `Option`, and exhaustive matching.
3. [Purity and Effect Types](/opt/prism-docs/tutorial/effects.md) makes observation visible in types and explains effect rows and row polymorphism.
4. [Handlers and Continuations](/opt/prism-docs/tutorial/continuations.md) shows how a handler controls the rest of a computation.
5. [Coeffects](/opt/prism-docs/tutorial/coeffects.md) turns usage and resource promises into compile-time contracts.
6. [Lenses and Streams](/opt/prism-docs/tutorial/lenses-streams.md) scales immutable updates and collection pipelines.
7. [Projects and Content Identity](/opt/prism-docs/tutorial/projects-identity.md) assembles a package, modules, tests, and Prism's content-addressed view of code.
8. [The Prism Way](/opt/prism-docs/tutorial/prism-way.md) collects the themes into practical working habits.

One honest warning: Prism is an active language project, not a production ecosystem. Explore it, steal ideas from it, and expect a few sharp experimental edges as it evolves.

**Further reading:** [language goals](/opt/prism-docs/spec.md#goal), [command-line interface](/opt/prism-docs/compiler.md#command-line-interface), and [diagnostics](/opt/prism-docs/compiler.md#diagnostics).

---

<!-- Source: docs/src/tutorial/functions.md -->

# Functions and Values

Python functions usually contain a sequence of statements and an explicit `return`. Prism functions are expressions: evaluate the body and its final value is the result.

## Bind values instead of assigning variables

Here is the same small calculation in both languages:



```prism
fn square(n : Int) : Int = n * n

fn main() =
  let side = 6
  let area = square(side)
  println("area = {area}")
```

```output
area = 36
```



```python
def square(n: int) -> int:
    return n * n


side = 6
area = square(side)
print(f"area = {area}")
```



`let` does not create a cell waiting to be reassigned. It gives a value a name. That makes data flow local: after reading `let area = square(side)`, you never have to search the rest of the function for a later `area = ...`.

Prism does support scoped mutation with `var` when it is the clearest way to write an algorithm. It is not the default vocabulary for ordinary data flow. Begin with `let` and introduce a `var` only when changing one place over time is the idea you mean.

## Types describe, inference fills the gaps

The annotation in

```prism
fn square(n : Int) : Int = n * n
```

says that `square` accepts one `Int` and returns an `Int`. Prism can infer this particular signature, so this is valid too:

```prism
fn square(n) = n * n

fn main() = println(square(7))
```

```output
49
```

Annotations are most useful at an API boundary or when they explain intent. They are checked descriptions, not runtime conversions: annotating a `String` as `Int` does not coerce it.

## Control flow produces values

Python's `if` chooses which statements to execute. Prism's `if` also chooses a value:

```prism
fn temperature_word(c : Int) : String =
  if c < 10 then
    "cold"
  elif c < 20 then
    "mild"
  else
    "warm"

fn main() =
  let word = temperature_word(18)
  println(word)
```

```output
mild
```

There is no `return` in the branches. Every branch must produce a compatible type because the entire `if` occupies one position in the program.

```prism,compile_fail
fn inconsistent(flag : Bool) =
  if flag then 1 else "one"
```

The failing example cannot choose between returning `Int` and returning `String`. Catching that disagreement at the branch is much better than letting it travel through later code.

## Functions are ordinary values

Python programmers already pass functions to `sorted`, `map`, decorators, and callbacks. Functional programming makes that habit central.

A Prism lambda is an unnamed function written `\(arguments) -> expression`:

```prism
fn apply_twice(f : (Int) -> Int, n : Int) : Int = f(f(n))

fn main() =
  let add_three = \(n) -> n + 3
  println(apply_twice(add_three, 10))
```

```output
16
```

The type `(Int) -> Int` describes a function value. `apply_twice` knows nothing about the implementation of `f`. Its type supplies everything needed to call it.

`map` applies a function to every element without changing the original list:

```prism
fn square(n : Int) : Int = n * n

fn main() =
  let numbers = [1, 2, 3, 4]
  let squares = map(square, numbers)
  println(show(numbers))
  println(show(squares))
```

```output
[1, 2, 3, 4]
[1, 4, 9, 16]
```

If a Python loop exists only to append one transformed value per input, `map` or a comprehension states the same intent without managing an accumulator.

## Composition and left-to-right reading

Small functions become useful when they compose. `f >> g` builds a function that runs `f` and then `g`. `x |> f` sends an existing value through a function:

```prism
fn double(n : Int) : Int = n * 2

fn add_one(n : Int) : Int = n + 1

fn main() =
  let double_then_add_one = double >> add_one
  println(double_then_add_one(20))
  println(20 |> double_then_add_one)
```

```output
41
41
```

Prism also permits `value.function(args)` as left-to-right call syntax, but it is only syntax: Prism has top-level functions rather than Python-style methods. `value.f(x)` means `f(value, x)`.

> **Try it:** Write `classify : (Int) -> String` using `if`. Then map it over `[-2, 0, 5]`. Before running the program, predict the inferred type of the resulting list.

## Checkpoint

You are ready to move on when these statements feel natural:

- a function body is an expression whose final value is its result.
- `let` names immutable data.
- annotations state facts that inference must satisfy.
- a function can be passed, returned, or stored like any other value.

Next, [Data and Patterns](/opt/prism-docs/tutorial/data.md) replaces open-ended object conventions with types that enumerate their valid shapes.

**Further reading:** [functions](/opt/prism-docs/spec.md#functions), [`let` statements](/opt/prism-docs/spec.md#let-statements), and [function composition](/opt/prism-docs/spec.md#function-composition).

---

<!-- Source: docs/src/tutorial/data.md -->

# Data and Patterns

Python offers lists, tuples, dictionaries, dataclasses, enums, inheritance, and `None`. That flexibility is convenient, but it can leave basic questions to runtime: which fields exist, which alternatives are possible, and whether every case was handled.

Prism builds those answers into data types.

## Lists, tuples, and comprehensions

A list contains values of one type. A tuple has a fixed number of positions whose types may differ:

```prism
fn main() =
  let colours = ["red", "green", "blue"]
  let reading = ("violet", 42)
  let (name, value) = reading
  println("{name}: {value}")
  println(show(colours))
```

```output
violet: 42
["red", "green", "blue"]
```

The pattern `(name, value)` destructures the tuple. It does not index into an unknown object. The type establishes that the pair has exactly two positions.

List comprehensions look familiar, but their source is a stream. This one collects five squares into a list:

```prism
fn main() =
  let squares = [n * n for n in srange(1, 6)]
  println(show(squares))
```

```output
[1, 4, 9, 16, 25]
```

Later we will keep a pipeline as a stream instead of collecting it. For now, use `map` or a comprehension when the result you want is another list.

## Records are named products

A Python dataclass says that one value contains several named fields. A Prism record says the same thing without attaching methods or an inheritance tree:

```prism
type Colour = Colour { name: String, wavelength: Int }

fn describe(c : Colour) : String = "{c.name}: {c.wavelength}nm"

fn main() =
  let violet = Colour { name = "violet", wavelength = 400 }
  let shifted = Colour { ..violet, wavelength = 405 }
  println(describe(violet))
  println(describe(shifted))
```

```output
violet: 400nm
violet: 405nm
```

`Colour { ..violet, wavelength = 405 }` creates an updated value. It does not mutate `violet`, which remains available with wavelength `400`.

A record is a **product** because one `Colour` contains a name _and_ a wavelength. A tuple is also a product. Records simply name the positions.

## Algebraic data types enumerate alternatives

Suppose a reading is either visible, infrared, or invalid. In Python you might use an enum plus optional payload fields, several dataclasses behind a union, or a string tag and conventions. Prism declares the complete vocabulary directly:

```prism
type Reading
  = Visible(String, Int)
  | Infrared(Int)
  | Invalid

fn describe(reading : Reading) : String =
  match reading of
    Visible(name, wavelength) => "{name} at {wavelength}nm"
    Infrared(wavelength) => "infrared at {wavelength}nm"
    Invalid => "invalid reading"

fn main() =
  println(describe(Visible("red", 700)))
  println(describe(Infrared(900)))
  println(describe(Invalid))
```

```output
red at 700nm
infrared at 900nm
invalid reading
```

`Reading` is a **sum** because a value has one shape _or_ another. Each constructor determines its payload. `Visible` always carries a `String` and an `Int`, while `Invalid` carries nothing. An invalid mixture of fields cannot be constructed.

This is the first major functional-programming habit: design the valid shapes first, then let functions consume those shapes.

## Patterns destructure and prove

A pattern performs two jobs at once. In `Visible(name, wavelength)`, it proves that the value is the `Visible` alternative and gives names to its two fields. The names exist only in that arm.

A `match` must cover every constructor:

```prism,compile_fail
type Reading
  = Visible(String, Int)
  | Infrared(Int)
  | Invalid

fn describe(reading : Reading) : String =
  match reading of
    Visible(name, wavelength) => "{name} at {wavelength}nm"
    Invalid => "invalid reading"
```

The missing `Infrared` arm is a compiler error. If you later add an `Ultraviolet` constructor, every incomplete match points to code whose policy must be reconsidered.

Patterns also work for literals, tuples, lists, and records. `_` means “this shape is possible, but I do not need its value”:

```prism
fn first_or(xs : List(Int), fallback : Int) : Int =
  match xs of
    Nil => fallback
    Cons(first, _rest) => first

fn main() =
  println(first_or([], 9))
  println(first_or([3, 4, 5], 9))
```

```output
9
3
```

For a record constructor, `C { .. }` ignores all of its fields at once. It is the concise form for an arm that cares which constructor matched, but not what that constructor carries:

```prism
type Packet = Data { bytes: List(Int) } | End { code: Int }

fn finished(packet : Packet) : Bool =
  match packet of
    Data { .. } => false
    End { .. } => true
```

## `Option` makes absence explicit

Python's `None` can appear wherever an object was expected, whether or not the annotation admitted it. Prism uses the ordinary algebraic data type `Option(a)`, whose alternatives are `None` and `Some(a)`:

```prism
fn visible_name(reading : Reading) : Option(String) =
  match reading of
    Visible(name, _wavelength) => Some(name)
    Infrared(_wavelength) => None
    Invalid => None

fn name_or_unknown(name : Option(String)) : String =
  match name of
    Some(value) => value
    None => "unknown"

fn main() =
  println(name_or_unknown(visible_name(Visible("green", 550))))
  println(name_or_unknown(visible_name(Infrared(900))))

type Reading = Visible(String, Int) | Infrared(Int) | Invalid
```

```output
green
unknown
```

`Option(String)` announces absence to every caller. Accessing the string requires handling `Some` and `None`. There is no stray null reference to fail somewhere unrelated.

> **Try it:** Add `Ultraviolet(Int)` to `Reading`. Let the compiler show every match that became incomplete, then decide separately what `describe` and `visible_name` should do with it.

## Checkpoint

You are ready to continue when you can explain:

- a record is one shape containing several fields.
- an algebraic data type is a closed set of possible shapes.
- a constructor builds one shape and a pattern takes it apart.
- exhaustiveness turns a data-model change into a useful list of affected code.

Next, [Purity and Effect Types](/opt/prism-docs/tutorial/effects.md) moves from the shapes of values to the observable actions computations may perform.

**Further reading:** [algebraic data types](/opt/prism-docs/spec.md#algebraic-data-types), [records](/opt/prism-docs/spec.md#record-types), [patterns](/opt/prism-docs/spec.md#patterns), and [comprehensions](/opt/prism-docs/spec.md#comprehensions).

---

<!-- Source: docs/src/tutorial/effects.md -->

# Purity and Effect Types

Python's `-> str` says what a function returns. It does not say whether the function also prints, reads a file, mutates global state, draws randomness, or raises an exception.

Prism gives a computation two descriptions:

- its value type says what it returns.
- its **effect row** says what it may observe or perform along the way.

## Pure computation versus observation

Consider a tiny report with a pure core and an effectful boundary:

```prism
fn band(wavelength : Int) : String =
  if wavelength < 450 then
    "violet"
  elif wavelength < 495 then
    "blue"
  elif wavelength < 570 then
    "green"
  else
    "warm"

fn report(wavelength : Int) : Unit ! {IO} =
  println("{wavelength}nm is {band(wavelength)}")

fn main() = report(530)
```

```output
530nm is green
```

`band` has no effect row because it only computes a `String` from an `Int`. Calling it again with the same argument supplies no new information. `report` returns only `Unit`, but its `! {IO}` says it may interact with the outside world.

In this tutorial, **pure** means “no outward observable effects.” It does not mean “does no work” or “allocates nothing.” Allocation promises belong to coeffects, and termination is a separate question.

Purity is useful rather than ceremonial. Pure code is easy to call from tests, simulations, optimizers, or several effectful front ends because it has no hidden world to reconstruct.

## Rows compose through ordinary calls

An effect row is a set of named capabilities. If a function calls something with an effect, that effect becomes part of the caller's row unless it is handled locally.

Errors demonstrate the rule with familiar control flow:

```prism
error InvalidWavelength(Int)

fn validate(wavelength : Int) : Int ! {InvalidWavelength} =
  if wavelength >= 380 && wavelength <= 750 then
    wavelength
  else
    throw InvalidWavelength(wavelength)

fn label(wavelength : Int) : String ! {InvalidWavelength} =
  "visible: {validate(wavelength)}nm"

fn safe_label(wavelength : Int) : String =
  try
    label(wavelength)
  catch
    InvalidWavelength(bad) => "outside the visible range: {bad}"

fn main() =
  println(safe_label(550))
  println(safe_label(900))
```

```output
visible: 550nm
outside the visible range: 900
```

`validate` may throw `InvalidWavelength`, so `label` inherits that label by calling it. `safe_label` catches every `InvalidWavelength`, so the label is absent outside the `try`/`catch`. Handling is **subtractive**: a fully interpreted effect disappears from the outward contract.

Compare that with Python. A Python caller must read documentation or inspect the body to learn which exceptions may escape. In Prism, unhandled errors are part of the same composition rule as every other effect.

## Define a capability as a typed question

An effect declares operations a computation may request. The computation calls an operation directly. A surrounding handler chooses its meaning:

```prism
effect Ask
  ask_word() : String

fn slogan() : String ! {Ask} =
  "Continuations are {ask_word()}!"

fn main() =
  let message =
    handle slogan() with
      ask_word() resume k => k("programmable")
      return value => value
  println(message)
```

```output
Continuations are programmable!
```

`slogan` neither receives a callback nor looks up ambient state. It asks the named `Ask` capability for a `String`, and its type records that dependency. The handler answers this run with `"programmable"` and resumes the suspended computation as `k`.

This separation is deeper than dependency injection. The handler can provide an answer, record the request, reject it, replay an earlier answer, or resume the request more than once. The next chapter explains that control explicitly.

## Closed and open rows

`! {Ask, IO}` is a **closed row**: it names the complete set of effects admitted there. Higher-order code often should not care which effects its function argument performs. It should preserve them.

```prism
effect Tick
  tick() : Int

fn twice(f : (Int) -> Int ! {| e}, x : Int) : Int ! {| e} =
  f(x) + f(x)

fn plus_one(n : Int) : Int = n + 1

fn plus_tick(n : Int) : Int ! {Tick} = n + tick()

fn main() =
  println(twice(plus_one, 20))
  let answer =
    handle twice(plus_tick, 20) with
      tick() resume k => k(1)
      return value => value
  println(answer)
```

```output
42
42
```

The row variable `e` means “whatever effects `f` has.” For `plus_one`, `e` is empty. For `plus_tick`, it contains `Tick`. `twice` does not erase, reinterpret, or add to that row. It passes the caller's effect information through.

This is **row polymorphism**. Ordinary polymorphism abstracts over a value type, such as “a list of any element type.” Row polymorphism abstracts over an open set of labels. It is what lets reusable wrappers remain honest without fixing one global stack of effects.

> **Try it:** Add an effect `Trace` with `trace(String) : Unit`. Write a function that traces and returns its argument, pass it to `twice`, and handle `Trace` in `main`. Watch the inferred row grow and then disappear at the handler.

## Checkpoint

You are ready to continue when you can read `(Int) -> String ! {Ask, InvalidWavelength}` as:

> accepts an `Int`, returns a `String`, and may ask a question or reject a wavelength before it returns.

You should also be able to explain why a handler can remove a label and why an open row lets higher-order code preserve effects it does not understand.

Next, [Handlers and Continuations](/opt/prism-docs/tutorial/continuations.md) examines the `k` that the handler receives.

**Further reading:** [effects and handlers](/opt/prism-docs/spec.md#effects-and-handlers), [errors and failure](/opt/prism-docs/spec.md#errors-and-failure), [effect observability](/opt/prism-docs/spec.md#observability), and [effect polymorphism](/opt/prism-docs/spec.md#effect-polymorphism).

---

<!-- Source: docs/src/tutorial/continuations.md -->

# Handlers and Continuations

The word **continuation** sounds abstract, but it names a concrete thing: what the program will do next.

In Python, a suspended generator remembers where to continue. Prism generalizes that idea. When an effect operation reaches a handler, Prism packages the slice of computation between the operation and that handler as a function-like value called the continuation.

## Find the rest of the computation

Imagine evaluating this expression:

```text
1 + choose() * 10
```

When `choose()` runs, the remaining recipe is:

```text
take the answer, select a number, multiply it by 10, then add 1
```

A handler names that recipe `k`:

```text
handle ... choose() ... with
  choose() resume k => ...
```

If `choose` returns a `Bool`, then `k(true)` resumes the recipe as though the operation returned `true`. The `handle` is a **delimiter**: it captures only the work back to this handler, not the entire operating-system stack.

## Resume once

The `Ask` handler from the previous chapter resumes once:

```prism
effect Ask
  ask_number() : Int

fn calculate() : Int ! {Ask} = 1 + ask_number() * 10

fn main() =
  let answer =
    handle calculate() with
      ask_number() resume k => k(4)
      return value => value
  println(answer)
```

```output
41
```

At the operation, `calculate` is paused. `k(4)` inserts `4` as the operation's result and finishes the pending arithmetic. The `return` clause describes what to do when the handled computation finishes normally.

## Resume zero times

A handler may discard the continuation. That abandons everything after the operation inside the delimiter, which is the control behavior behind an exception or early rejection:

```prism
effect Abort
  never abort(String) : String

fn checked_name(name : String) : String ! {Abort} =
  if name == "" then
    abort("empty name")
  else
    "hello {name}"

fn safe_name(name : String) : String =
  handle checked_name(name) with
    never abort(message) => "invalid: {message}"
    return value => value

fn main() =
  println(safe_name("Ada"))
  println(safe_name(""))
```

```output
hello Ada
invalid: empty name
```

`never` records that an `abort` handler must not resume. In the invalid branch, the handler's string becomes the result of the whole `handle` expression.

## Resume more than once

Calling the same continuation with several answers explores several futures of one otherwise ordinary computation:

```prism
effect Choice
  choose() : Bool

fn price() : Int ! {Choice} =
  if choose() then 10 else 20

fn main() =
  let worlds =
    handle price() with
      choose() resume k => append(k(true), k(false))
      return value => [value]
  println(show(worlds))
```

```output
[10, 20]
```

`price` returns one `Int` and does not know that a search is happening. The handler decides to run the remainder once with `true` and again with `false`. The return clause wraps each completed result in a list, so each `k(...)` returns a list and `append` combines the possible worlds.

This is a **multishot continuation**. A Python generator can yield several values, but the producer must be written as a generator. Here the computation only asks a typed question. The handler decides whether that question means a fixed answer, interactive input, replay, search, or something else.

## Grades make resumption promises explicit

Every effect operation has a resumption grade:

| Grade   | What a handler may do with the continuation    |
| ------- | ---------------------------------------------- |
| `never` | discard it and do not resume                   |
| `once`  | resume exactly once in tail position           |
| `many`  | capture it and resume zero, one, or many times |

`many` is the default, which is why `Choice.choose` may resume twice. A stronger grade lets the compiler reject a handler that violates the operation's control contract:

```prism,compile_fail
effect Read
  once read() : Int

fn query() : Int ! {Read} = read() + 1

fn invalid_handler() : Int =
  handle query() with
    read() resume k => k(10) + k(20)
    return value => value
```

The declaration promises one tail-position resumption, but the handler tries to use `k` twice. This is checked statically rather than left as a comment about a callback.

## One mechanism, several familiar features

Handlers change policy without rewriting the computation:

- resuming zero times gives abort, failure, and pruning.
- resuming once can supply configuration, state, logging, or simulation time.
- resuming later gives coroutines and schedulers.
- resuming several times gives search and nondeterminism.

The important boundary is always the same: the computation names what it needs, and the nearest matching handler decides what that request means.

> **Try it:** Change the `Choice` handler to resume only with `true`. Then change it to `append(k(false), k(true))`. Notice that `price` never changes. Only the interpretation and result order do.

## Checkpoint

You are ready to continue when you can point to an operation and describe `k` as “the rest of the computation up to this handler,” then predict what happens when the handler calls it zero, one, or several times.

Next, [Coeffects](/opt/prism-docs/tutorial/coeffects.md) moves from what a computation may do to how a value may be used.

**Further reading:** [effects and handlers](/opt/prism-docs/spec.md#effects-and-handlers), [operation grades](/opt/prism-docs/spec.md#three-posets), and [effect lowering](/opt/prism-docs/compiler.md#effect-lowering).

---

<!-- Source: docs/src/tutorial/coeffects.md -->

# Coeffects

An effect describes what may happen while a computation runs. A **coeffect** describes how the surrounding program may use a value, or which resource property was required to produce it.

A useful reading rule is:

- `!` reports outward: “this computation may perform these effects.”
- `@` demands inward: “use this value only under these conditions.”

Python has no direct equivalent. A decorator can perform a runtime check and a type-checker plugin can enforce a convention, but neither makes these promises part of the ordinary function type.

## Certify an allocation property

`@ noalloc` promises that evaluation of the function's whole call tree allocates no fresh heap cell:

```prism
fn gcd(a : Int, b : Int) : Int @ noalloc =
  if b == 0 then
    a
  else
    gcd(b, a % b)

fn main() = println(gcd(48, 18))
```

```output
6
```

Integer arithmetic and this recursion satisfy the promise. Constructing a fresh list inside `gcd` would not. The annotation is not an optimization hint. It is a claim the compiler checks.

This sharpens the meaning of purity from the previous chapter. A pure function has no outward observable effect, but it may still allocate. `@ noalloc` certifies the stronger and separate resource property.

The same fact can be demanded of a callable. Written on a function-typed parameter, `@ noalloc` obliges every argument supplied for it to carry the certificate:

```prism
fip fn step(n : Int) : Int = n + 1

fn iterate(f : ((Int) -> Int) @ noalloc, x : Int) : Int = f(f(x))

fn main() = println(iterate(step, 40))
```

```output
42
```

An uncertified callable cannot flow into the demanding slot:

```prism,compile_fail
fn boxed(n : Int) : List(Int) = [n]

fn demand(f : ((Int) -> List(Int)) @ noalloc, x : Int) : List(Int) = f(x)

fn main() = println(demand(boxed, 1))
```

Passing `step` to an ordinary parameter needs no annotation. Forgetting the fact is free; only a demanding slot asks for proof.

## Constrain how a function value is consumed

`@ once` on a function value says that its receiver consumes it at most once:

```prism
fn apply_once(f : ((Int) -> Int) @ once, value : Int) : Int =
  f(value)

fn main() =
  println(apply_once(\(n) -> n * 2, 21))
```

```output
42
```

The contract belongs to `apply_once`, not to the lambda. It promises callers that the callback will not be duplicated or retained for a second use. Breaking that promise is a type error:

```prism,compile_fail
fn apply_once(f : ((Int) -> Int) @ once, value : Int) : Int =
  f(value) + f(value)
```

Python can write “called at most once” in a docstring or wrap the callback in a runtime guard. Prism makes the restriction visible before the program runs.

The unrestricted default has a spelled form, `@ many`: it admits repeated use, is exclusive with `once`, and a `once` value never fits a `many` slot (the reverse always does).

## The checked vocabulary

Prism currently checks seven coeffects:

| Coeffect        | Promise                                                               |
| --------------- | --------------------------------------------------------------------- |
| `noalloc`       | evaluation allocates no fresh heap cell                               |
| `linear`        | no owned heap input is duplicated across the certified call tree      |
| `bounded_stack` | the certified call tree runs in bounded stack                         |
| `once`          | a value is consumed at most once                                      |
| `many`          | a value may be consumed freely (the spelled default)                  |
| `portable`      | a value carries only state safe to move across the supported boundary |
| `noescape`      | a borrowed value does not escape its permitted scope                  |

Coeffects are compile-time contracts and are erased before execution. They do not perform operations and they do not need handlers.

## Effects, grades, and coeffects are different views

These features are related but not interchangeable:

| Question                                 | Prism feature                               |
| ---------------------------------------- | ------------------------------------------- |
| What may this computation do?            | effect row, such as `! {IO, Ask}`           |
| How may this handler resume?             | operation grade: `never`, `once`, or `many` |
| How may this value be consumed?          | usage coeffect, such as `@ once`            |
| What resource fact holds for evaluation? | resource coeffect, such as `@ noalloc`      |

The connection becomes concrete at a handler clause. The continuation `k` is a value, and an operation grade constrains how the handler may consume it. A `once` operation therefore gives its continuation a checked one-use discipline. A `many` operation permits capture and duplication.

> **Try it:** Add `let xs = [a, b]` inside `gcd` and return `a` as before. The list is unused, but its allocation still violates `@ noalloc`. Remove the annotation and compare the inferred function type.

## Checkpoint

You are ready to continue when “pure” and “does not allocate” no longer sound like synonyms, and when you can distinguish a computation's effect row from a value's usage contract.

Next, [Lenses and Streams](/opt/prism-docs/tutorial/lenses-streams.md) uses these functional foundations to update nested immutable data and process large sequences.

**Further reading:** [coeffects and usage rows](/opt/prism-docs/spec.md#usage-and-resource-annotations), [allocation certificates](/opt/prism-docs/spec.md#allocation-certificates), and [the three posets](/opt/prism-docs/spec.md#three-posets).

---

<!-- Source: docs/src/tutorial/lenses-streams.md -->

# Lenses and Streams

Immutability removes the need to track surprise mutation, but two practical questions follow:

1. How do you update something deeply nested without rebuilding every layer by hand?
2. How do you transform a large sequence without allocating a fresh list after every step?

Lenses and streams answer those questions.

## From nested copies to a focus

Python's frozen dataclasses can be updated with `dataclasses.replace`, but a deep update repeats every layer:

```python
moved = replace(player, position=replace(player.position, x=9))
```

Prism records have ordinary functional update syntax, and an **optic path** composes the nested focus:

```prism
type Vec2 = Vec2 { x: Int, y: Int }

type Player = Player { name: String, position: Vec2, score: Int }

fn main() =
  let player =
    Player {
      name = "Ada",
      position = Vec2 { x = 1, y = 2 },
      score = 10
    }
  let moved = { player | position.x = 9, score += 5 }
  println(moved.position.x)
  println(moved.score)
  println(player.position.x)
```

```output
9
15
1
```

The path `position.x` focuses an `Int` inside a `Vec2` inside a `Player`. The update returns a `Player`. The original still contains `x = 1`. When ownership is unique, the compiler may reuse the old storage in place without changing that functional meaning.

## A lens is a reusable single focus

Conceptually, a lens packages two operations:

- view one part of a larger value.
- return the larger value with that part replaced.

`deriving (Lens)` generates checked getters and functional setters for a record:

```prism
type Vec2 = Vec2 { x: Int, y: Int } deriving (Lens)

fn main() =
  let point = Vec2 { x = 3, y = 4 }
  let moved = with_x(point, 12)
  println(x_of(point))
  println(x_of(moved))
```

```output
3
12
```

The generated `x_of` and `with_x` are ordinary functions. Optic paths provide the concise surface syntax for composing such focuses through real data.

The idea generalizes:

- a **lens** focuses exactly one field.
- a **prism** focuses one constructor of an algebraic data type.
- a **traversal** focuses zero or more elements.

For example, `each` traverses a list, and `~` modifies every focus with a function:

```prism
type Player = Player { name: String, score: Int }

fn bonus(score : Int) : Int = score + 10

fn main() =
  let players = [
      Player { name = "Ada", score = 30 },
      Player { name = "Grace", score = 40 },
    ]
  let rewarded = { players | each.score ~ bonus }
  println(rewarded.[each.score])
```

```output
[40, 50]
```

The path says what to focus. `=`, `~`, and compound updates say what to do there. That separates navigation from policy.

## Lists are values and streams are processes

Each call in this list pipeline produces another complete list:

```text
numbers -> mapped list -> filtered list -> first five -> sum
```

Python often replaces those intermediates with generator expressions or `itertools`. Prism uses streams. Although evaluation is strict, stream transformers are fused between a producer and a consumer:

```prism
fn square(n : Int) : Int = n * n

fn main() =
  let total =
    srange(1, 1000)
      .smap(square)
      .skeep(even)
      .stake(5)
      .ssum()
  println(total)
```

```output
220
```

Read the chain from left to right:

1. produce integers beginning at `1`.
2. square each integer.
3. keep the even squares.
4. stop after five values.
5. sum them.

No list of 999 integers or intermediate squares is required. `stake(5)` also stops the source early, so later values are never requested.

`sof(xs)` turns a list into a stream. `scollect()` consumes a stream into a list when a materialized result is actually wanted:

```prism
fn main() =
  let values = sof([1, 2, 3, 4]).smap(\(n) -> n * 3).scollect()
  println(show(values))
```

```output
[3, 6, 9, 12]
```

## Streams are another use of handlers

A Prism stream is a producer that emits values through an effect. Transformers handle each emission and emit a transformed stream. Consumers handle emissions and fold them into a final value. The compiler can lower the composed handlers to a fused state-threading loop.

This connects the feature to the previous chapters:

- `smap` resumes once for each transformed value.
- `skeep` may drop a value while continuing the source.
- `stake` stops early by discarding the remaining continuation.

The user-facing pipeline stays direct and declarative. Effects and continuations explain why its control flow can be composed and optimized.

> **Try it:** Change the stream to cube the numbers, keep odd results, and take three. Predict which source value is the last one requested. Then replace `ssum()` with `scollect()` and inspect the values.

## Checkpoint

You are ready to continue when you can explain an optic as a composable focus and a stream as a producer/consumer process rather than an already-built collection.

Next, [Projects and Content Identity](/opt/prism-docs/tutorial/projects-identity.md) puts the language ideas into a package and shows how Prism decides whether code is still the same code.

**Further reading:** [optic paths](/opt/prism-docs/spec.md#optic-paths), [derived lenses](/opt/prism-docs/spec.md#deriving-lens), [streams](/opt/prism-docs/spec.md#streams), and [effect lowering](/opt/prism-docs/compiler.md#effect-lowering).

---

<!-- Source: docs/src/tutorial/projects-identity.md -->

# Projects and Content Identity

A tutorial should end with more than isolated snippets. This chapter assembles a small package, splits it into modules, runs its tests, and then inspects the content identities Prism assigns to its definitions.

## Create the project

Run the interactive initializer and choose `rainbow` as the package and directory name. Use `0.1.0` for the version, your own author and maintainer details, and the license you intend to publish under:

```shell
prism pkg init
cd rainbow
```

The project begins with a manifest and one source file:

```text
rainbow/
├── prism.toml
└── src/
    └── main.pr
```

Use this manifest:

```toml
[package]
name = "rainbow"
version = "0.1.0"
authors = ["Your Name <you@example.com>"]
maintainers = ["you@example.com"]
license = "MIT"

[bin]
entry = "src/main.pr"
```

## Put pure domain logic in a module

Create `src/Colour.pr`:

```prism,no_run
pub type Band
  = Violet
  | Blue
  | Green
  | Warm
  deriving (Eq, Show)

pub fn band(wavelength : Int) : Band =
  if wavelength < 450 then
    Violet
  elif wavelength < 495 then
    Blue
  elif wavelength < 570 then
    Green
  else
    Warm

pub fn name(value : Band) : String =
  match value of
    Violet => "violet"
    Blue => "blue"
    Green => "green"
    Warm => "yellow, orange, or red"

test fn green_is_classified() =
  if band(530) == Green then
    ()
  else
    fail()

test fn warm_name_is_readable() =
  if name(Warm) == "yellow, orange, or red" then
    ()
  else
    fail()
```

A file is a module. `pub` exports a declaration. Declarations without `pub` are private to that file. The tests can still use private declarations in their own module.

Now replace `src/main.pr`:

```prism,ignore
import Colour (band, name)

fn main() =
  let wavelength = 530
  let result = band(wavelength)
  println("{wavelength}nm is {name(result)}")
```

The imported names come from `src/Colour.pr`. An unqualified `import Colour` would keep them behind `Colour.band`. `import Colour (band, name)` also brings those two exports into bare scope.

The complete project is checked in with Prism's documentation examples, so the multi-file version is tested even though this individual import block cannot run on its own.

## Check, test, and run

From the project directory:

```shell
prism fmt src
prism check
prism test
prism run .
```

The final command prints:

```output
530nm is green
```

`prism test` discovers `test fn` declarations throughout project-owned modules. A test passes by returning `Unit`. Calling `fail()` or leaving a fault unhandled fails it. Each test receives a fresh interpreter world, so effects and state cannot leak from one test to another.

Tests are deliberately production-neutral. Normal builds remove test declarations before module interfaces, executable Core identities, and native artifacts are computed. Improving a test does not change the program being shipped.

## Read a diagnostic as a workflow

Change `Green` to the misspelling `Gren` in the first test and run `prism test`. A useful diagnostic answers three questions:

1. Where is the problem?
2. What fact did the compiler expect?
3. Is there a mechanical repair it can suggest?

Prism diagnostics include a stable `E`-code. Run `prism explain CODE` with the printed code for a longer explanation and a minimal fixed example. Then restore `Green` and rerun the test.

For an unfinished expression, a named typed hole such as `?result` asks a slightly different question: what type and effects must fit here?

```prism,ignore
fn double(n : Int) : Int = ?result
```

On a local file, `prism check FILE --at-hole` reports the expected type, permitted effects, and matching values in scope. Holes make incomplete code a structured compiler query rather than a comment the compiler cannot see.

## Source text is not semantic identity

Most tools identify a function by a filename and name, or cache it using a hash of its source bytes. That makes comments, formatting, and local variable names look like behavioral changes.

Prism hashes the elaborated, pre-optimization Core instead:

```shell
prism dump core-hash .
```

Try this small experiment:

1. Save the reported hashes.
2. Rename `wavelength` to `nm` inside `band`, or change only a comment.
3. Format the file and dump the hashes again.
4. Change the boundary `450` to `451` and dump them a third time.

Bound variables are normalized by position, while comments, source spans, and formatting are erased. The first edit therefore leaves `band`'s Core identity unchanged. The numerical edit changes what the definition computes, so its hash moves.

You do not normally need to compare those hash dumps by hand. Put the starting version in Git, make the same edits, and ask Prism for the semantic diff:

```shell
git init
git add prism.toml src
git commit -m "Start rainbow"

# Edit src/Colour.pr, then:
prism diff
```

Bare `prism diff` compares Git `HEAD` with the whole working tree, including staged changes. It reports definitions whose Core identity changed, shows a compact source patch, and names the dependent definitions affected by the change. A comment-only or formatting-only edit has no semantic changes to report. To compare two explicit revisions instead, pass two files, project directories, or manifests: `prism diff OLD NEW`.

The hash commits to more than the expression tree. It also includes elaboration facts an importer relies on, including the generalized type, principal effect row, allocation mode, and borrow mask. Changing a public contract therefore changes identity even if a similar-looking body remains.

Those facts are inspectable too. The command for auditing the project-owned functions in the checked program is:

```shell
prism dump usage-summary .
```

For the rainbow project it includes:

```text
# prism-usage-summary-v1  tier=pure
# name        noalloc  discipline  borrow  row
Colour.band  no       -           -       {}
Colour.name  no       -           -       {}
main         no       -           -       {IO}
```

The final column is each function's checked effect row: `band` and `name` are pure, while `main` performs `IO`. The other columns expose the allocation certificate, `fip`/`fbip` discipline, and parameter borrow mask that also feed semantic identity. Use `usage-summary-md` for a Markdown table or `usage-summary-json` for tooling.

## Definitions form a Merkle graph

When one top-level definition refers to another, Prism places the dependency's hash into the caller's identity. A changed `band` therefore affects definitions that depend on its behavior, while an unrelated definition can retain its identity.

This turns the program into a Merkle graph:

```text
band hash ──▶ caller hash ──▶ module/package root
name hash ──▶ caller hash
```

Names remain a human-facing index over that graph. A top-level rename can leave the definition's anonymous Core object unchanged while moving the named namespace entry. Packages and the standard library are pinned by roots over their definitions and declared shapes:

```shell
prism dump stdlib-hash .
```

This supports precise rebuilds, reproducible package pins, lineage, and replay: unchanged content already has a stable name, and a change propagates through its dependency closure.

One limit matters. A Core hash is not a proof that all differently written programs with the same mathematical result will be recognized as equivalent. Equal identities name the same canonical compiler form under Prism's hashing scheme. Unequal identities do not prove that two programs could never behave the same.

## What you can now build

The project is intentionally small, but it contains the complete beginner workflow:

- immutable pure functions model the domain.
- an algebraic data type names every output category.
- pattern matching covers those categories.
- module visibility separates an API from its implementation.
- tests exercise the pure core.
- `main` owns the observable output boundary.
- content identities reveal which semantic artifacts changed.

Extend it by accepting several readings, processing them as a fused stream, and returning `Option(Band)` for wavelengths outside the visible range. Add a custom effect that supplies readings, then write one handler with fixed test data and another with a real input capability. That exercise revisits every major idea without changing the pure classification logic.

Next, [The Prism Way](/opt/prism-docs/tutorial/prism-way.md) condenses those ideas into habits worth carrying into larger programs.

**Further reading:** [modules](/opt/prism-docs/spec.md#modules), [projects](/opt/prism-docs/spec.md#projects), [test declarations](/opt/prism-docs/spec.md#test-declarations), [typed holes](/opt/prism-docs/compiler.md#typed-hole-workflow), and [content-addressed Core](/opt/prism-docs/compiler.md#content-addressed-core).

The future of code review will be less about staring at ever larger diffs and more about asking which guarantees moved. As LLMs make code cheap to produce, review becomes the scarce and critical act, and textual plausibility is no longer enough. Advanced static types can expose a change's effects and resource contracts, while a Merkle closure can trace those facts through every caller whose behavior depends on them. Prism brings these views together. `prism diff` identifies the semantic change and its affected cone, while the usage summary reveals the checked effect rows and resource promises inside that cone. The result is a review process built around static evidence about what code can do and where its consequences travel, which makes machine-generated changes far easier to audit than a raw patch ever could.

---

<!-- Source: docs/src/tutorial/prism-way.md -->

# The Prism Way

Every young language eventually announces a "way," the programming-language equivalent of arranging six ordinary rocks and calling the result a Zen garden. Prism continues the cliché, but takes it to the level of absurdity because this is a fun language, not a serious one!

There are six gates, numbered from zero because enlightenment naturally follows zero-indexing conventions as the universe intends:

0. **Shape the impossible.** If an invalid value cannot be constructed, it cannot cause a bug. If no values can be constructed, the type is perfect.
1. **The program is not the world.** Prism keeps asking which parts of reality belong in the computation and which parts should remain outside it. Enlightenment is not modelling everything. It is knowing what can be left out.
2. **State is an illusion.** Does the program mutate while the world holds still, or does the world mutate around a program that only remembers? Prefer expressions and immutable transformations. If mutation is the clearest description of the algorithm, permit it briefly, then let ownership return the object to silence.
3. **Name the world.** Effects are the labels on the doors through which reality enters. Handle a label and it leaves the outward contract. The universe has not become pure. It has merely been given a type.
4. **Detach from use.** Coeffects say how a value may be used: once, here, without escape, without allocation. The compiler checks the attachment, then invites the value to let go.
5. **Hash the emptiness.** Canonical Core gives behavior an identity, so caches, replay, diffs, and builds can ask whether a computation is still itself. The final question is whether it needed to exist at all.

> "Master, what is an effect?"
>
> "Anything that leaves the program."
>
> "Non-determinism?"
>
> "An effect."
>
> "Termination?"
>
> "An effect."
>
> "Allocation?"
>
> "An effect."
>
> "The world?"
>
> "Especially the world."
>
> "Existence?"
>
> "The first effect."
>
> The student purified the program until it had no effects left. It did not run, allocate, terminate, or exist. The compiler truncated the file to zero bytes.
>
> "Master, where did it go?"
>
> "It has achieved referential transparency."

The joke has a practical point. Real Prism programs read files, print output, allocate data, and cross named effect boundaries. Keep pure computation separate from observation, make resource promises explicit, and let the compiler erase what nobody needs. With the same code identity and recorded observations, Prism aims for the same result across the interpreter and native backends.

That is enough for a first tour. Continue with the [Language Specification](/opt/prism-docs/spec.md) when you want the precise rules, browse the [Standard Library](/opt/prism-docs/stdlib/index.md) for the available building blocks, and open the [Compiler](/opt/prism-docs/compiler.md) chapter when phrases like "canonical Core hash" start sounding less like a metaphor and more like an implementation question.

**Further reading:** [record and replay](/opt/prism-docs/spec.md#record-and-replay), [lineage](/opt/prism-docs/spec.md#lineage), [source, surface, and Core identity](/opt/prism-docs/compiler.md#three-identities), and [reference counting and in-place reuse](/opt/prism-docs/compiler.md#reference-counting-and-fbip-reuse).
