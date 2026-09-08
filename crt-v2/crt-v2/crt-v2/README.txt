CRT v2.1.2
==========
A tiny scripting language that compiles to CVM bytecode.

WINDOWS INSTALL
  Double-click install.bat (requires Python 3.8+ on PATH).
  It will ask if you also want pygame installed, for graphics scripts.

MANUAL INSTALL (any OS)
  pip install .
  pip install pygame        (only needed if you use pygame_* calls)
  # or in one step: pip install .[graphics]

USAGE
  crt run file.ct                Run a .ct source file directly
  crt build file.ct -o file.cvm  Compile to bytecode
  crt exec file.cvm              Execute compiled bytecode
  crt disasm file.cvm            Disassemble bytecode
  crt asm file.asm -o file.cvm   Assemble CRT assembly to bytecode
  crt                            Start the REPL
  crt demo                       Print the language cheat sheet
  crt demo graphics              Print the pygame/graphics cheat sheet
  crt demo asm                   Print the CRT assembly cheat sheet
  crt -h                         Full command list

LANGUAGE FEATURES
  - let, if / else if / else, while, for, break, continue
  - functions with recursion + mutual recursion
  - lists, objects (dot and [index] access + assignment)
  - int, float, string, bool, null
  - operators: + - * / % , == != < > <= >= , && || ! (and/or/not), unary -
  - // line comments and /* block comments */
  - string escapes: \n \t \r \" \' \\ \0
  - builtins: len, str, int, float, type, upper, lower

GRAPHICS MODE (pygame)
  Calling any pygame_* function opens a real window via pygame and enters
  "graphics mode". Nothing else changes about the language -- these are
  just builtin functions, usable anywhere a normal call is.

    pygame_init(width, height, title)   open the window
    pygame_quit()                        close the window
    pygame_clear()                       clear to black
    pygame_clear(r, g, b)                clear/fill the frame with a color
    pygame_rect(x, y, w, h, r, g, b)     draw a filled rectangle
    pygame_circle(x, y, radius, r, g, b) draw a filled circle
    pygame_line(x1, y1, x2, y2, r, g, b) draw a line
    pygame_text(x, y, text, r, g, b)     draw text
    pygame_flip()                        present the frame you drew
    pygame_poll_quit()                   -> true if the window's X was clicked
    pygame_key(name)                     -> true if that key is held down
                                            names: left right up down space
                                            enter escape shift ctrl tab, or
                                            any single letter/digit
    pygame_tick(fps)                     caps the framerate, returns the
                                          milliseconds since the last tick

  A typical game loop:

    pygame_init(640, 480, "My Game");
    let running = true;
    while (running) {
        if (pygame_poll_quit()) { running = false; }
        if (pygame_key("escape")) { running = false; }

        // ...update your state here...

        pygame_clear();
        pygame_circle(100, 100, 20, 255, 0, 0);
        pygame_flip();
        pygame_tick(60);
    }
    pygame_quit();

  See examples_bounce_demo.ct for a complete bouncing-ball demo you can
  run directly: crt run examples_bounce_demo.ct  (needs pygame installed)

  Colors are (r, g, b) 0-255. Coordinates accept floats and are truncated
  to whole pixels automatically, so `100 / 3` works fine as a coordinate.
  Calling any drawing function before pygame_init() gives a clear error
  instead of crashing.

CRT ASSEMBLY (hand-written bytecode)
  For people who want to write or generate CVM bytecode directly instead
  of going through the CRT language front-end:

    crt asm file.asm -o file.cvm   Assemble text -> a .cvm file, usable
                                    with crt exec / crt disasm exactly
                                    like one produced by crt build
    crt demo asm                   Full instruction list + examples

  Text format:
    ; a comment, to end of line
    label:                  define a jump target (address of the next
                             instruction)
    .func name arity        mark the next instruction's address as the
                             entry point of a callable function
    MNEMONIC                instruction with no operand
    MNEMONIC 42              integer operand
    MNEMONIC "text"           string operand
    MNEMONIC 3.5                float operand
    MNEMONIC label                 label used as a jump target
    MNEMONIC "name", 2               string operand + extra field, e.g.
                                      CALL "add", 2  (name, arg count)

  A `crt disasm` listing is close enough to valid assembly that editing
  one is a quick way to get started; undefined labels are caught as
  errors at assemble time rather than silently resolving to address 0.

WHAT'S NEW IN 2.1.2
  - New `crt asm` command: assembles hand-written CVM assembly text
    (see "CRT ASSEMBLY" above and `crt demo asm`) into a .cvm file, the
    same format `crt build` produces
  - `crt demo asm` now works (previously referenced in `crt demo` output
    but not wired up, so it just errored)
  - `crt -h` / `crt --help` now lists the asm subcommand
  - Fixed a harmless leftover dead-code line in the assembler's tokenizer

WHAT'S NEW IN 2.1.1
  - crt demo (and crt demo graphics): prints a cheat sheet
  - pygame_clear() with no args now clears to black as a shortcut

WHAT'S NEW IN 2.1.0
  - Graphics mode: pygame_init/clear/rect/circle/line/text/flip/
    poll_quit/key/tick, with compile-time argument-count checking (same
    as ordinary functions) and clean runtime errors (missing pygame,
    drawing before init, bad key names, negative radius, etc.)
  - New CALL_GFX opcode so scripts that never touch graphics have zero
    pygame dependency at runtime

WHAT'S NEW IN 2.0.x
  - Functions with recursion + mutual recursion (fn ... return ...)
  - for loops, break, continue
  - else if chains
  - Unary minus and logical not (-x, !x, not x)
  - Floats, modulo (%)
  - Line comments (//) and block comments (/* */)
  - Proper escape sequences in strings (\n \t \" etc.)
  - Line numbers in error messages
  - REPL now remembers functions you define across turns

BUG FIXES IN 2.0.x (over the original 1.1.0)
  - Recursive calls used to crash the VM entirely
  - Function arguments were being bound in the wrong order
    (e.g. fn f(a,b) called as f(10,0) would bind a=0, b=10)
  - Assigning to a variable from inside a function used to silently
    create a new local instead of updating the outer/global variable
    (a global counter incremented inside a function never actually
    changed)
  - Calling a function with the wrong number of arguments used to crash
    with a confusing "stack underflow" instead of a clear error
  - Bytecode format now stores a real function table (name, address,
    arity) instead of guessing call targets from call order, which
    broke for any function that called itself before being called from
    outside (.cvm format is now v4 -- old .cvm files must be rebuilt
    with `crt build`)
