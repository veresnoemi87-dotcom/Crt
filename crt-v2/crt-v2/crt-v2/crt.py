#!/usr/bin/env python3
"""
CRT - a tiny scripting language that compiles to CVM bytecode.

CRT (the language) compiles down to CVM ("Compact Virtual Machine")
bytecode: a simple stack machine with a binary instruction format you can
disassemble and inspect. This file contains the whole toolchain:

    1. Opcodes & ISA
    2. Lexer & Parser   (source text -> tokens -> AST)
    3. Compiler         (AST -> CVM bytecode)
    4. CVM Runtime      (bytecode -> execution)
    5. CLI / REPL / Disassembler

Language features: variables, numbers/strings/bools/null, lists, objects,
if/else if/else, while, for, break/continue, functions with return values,
and a handful of built-ins (len, str, int, float, type).

CLI usage:
    crt run file.ct                  Run a .ct source file directly
    crt build file.ct -o file.cvm    Compile to a .cvm bytecode file
    crt exec file.cvm                Execute a compiled .cvm file
    crt disasm file.cvm              Disassemble a compiled .cvm file
    crt asm file.asm -o file.cvm     Assemble CRT assembly to bytecode
    crt                               Start an interactive REPL
"""

import sys
import os
import struct
import re
import json
import argparse

# =====================================================================
# 1. OPCODES & ISA DEFINITION
# =====================================================================
#
# Each instruction is a fixed 8-byte record:
#   opcode   : u8   - which operation (see OPCODES)
#   arg_type : u8   - how to interpret arg_val (0=none, 1=immediate, 2=string index)
#   arg_val  : i32  - big-endian signed 32-bit immediate / string-table index / jump target
#   extra    : i16  - secondary small operand (currently only used by CALL for argc)

OPCODES = {
    "HALT":          0x00,
    "PUSH_INT":      0x01,
    "PUSH_STR":      0x02,
    "PUSH_BOOL":     0x03,
    "LOAD":          0x04,
    "STORE":         0x05,
    "ADD":           0x06,
    "SUB":           0x07,
    "MUL":           0x08,
    "DIV":           0x09,
    "EQ":            0x0A,
    "NEQ":           0x0B,
    "LT":            0x0C,
    "GT":            0x0D,
    "LTE":           0x0E,
    "GTE":           0x0F,
    "PRINT":         0x10,
    "JUMP":          0x11,
    "JUMP_IF_FALSE": 0x12,
    "MAKE_LIST":     0x13,
    "MAKE_OBJECT":   0x14,
    "GET_MEMBER":    0x15,
    "SET_MEMBER":    0x16,
    "GET_INDEX":     0x17,
    "SET_INDEX":     0x18,
    "MOD":           0x19,
    "NEG":           0x1A,
    "NOT":           0x1B,
    "PUSH_FLOAT":    0x1C,
    "PUSH_NULL":     0x1D,
    "POP":           0x1E,
    "DUP":           0x1F,
    "JUMP_IF_TRUE":  0x20,
    "CALL":          0x21,
    "CALL_BUILTIN":  0x22,
    "RET":           0x23,
    "MAKE_FRAME":    0x24,   # enter a new local scope, binding params (arg_val = number of params)
    "STORE_LOCAL":   0x25,   # always bind in the current scope (used for 'let' decls and params)
    "CALL_GFX":      0x26,   # call a pygame graphics builtin (arg_val = string index of name, extra = argc)
}

REV_OPCODES = {v: k for k, v in OPCODES.items()}

ARG_NONE, ARG_IMM, ARG_STR = 0, 1, 2

BUILTINS = (
    "len", "str", "int", "float", "type", "upper", "lower",
    # JSON
    "json_encode", "json_decode",
    # File I/O
    "file_read", "file_write", "file_append", "file_exists", "file_delete",
)

# Graphics builtins ("pygame mode"). Each maps to the argument count(s) it
# accepts -- an int for a fixed arity, or a tuple for a few allowed arities
# (pygame_clear can take 0 args, clearing to black, or 3 for an explicit
# color) -- so the compiler can catch wrong-arity calls the same way it
# does for ordinary functions. These compile to CALL_GFX instead of
# CALL_BUILTIN so the runtime only has to import pygame when a script
# actually uses one.
GFX_BUILTINS = {
    "pygame_init":        3,       # (width, height, title) -> null
    "pygame_quit":        0,       # () -> null
    "pygame_clear":       (0, 3),  # () clears to black, or (r, g, b) -> null
    "pygame_rect":        7,       # (x, y, w, h, r, g, b) -> null
    "pygame_circle":      6,       # (x, y, radius, r, g, b) -> null
    "pygame_line":        7,       # (x1, y1, x2, y2, r, g, b) -> null
    "pygame_text":        6,       # (x, y, text, r, g, b) -> null
    "pygame_flip":        0,       # () -> null
    "pygame_poll_quit":   0,       # () -> bool (true if window close was requested)
    "pygame_key":         1,       # (name) -> bool
    "pygame_tick":        1,       # (fps) -> float (ms elapsed since last tick)
    # Textures (loaded via Pillow, converted to a pygame surface)
    "pygame_load_image":  1,       # (path) -> int image handle
    "pygame_draw_image":  3,       # (handle, x, y) -> null
    "pygame_image_size":  1,       # (handle) -> {w: ..., h: ...}
    # Audio
    "pygame_load_sound":  1,       # (path) -> int sound handle
    "pygame_play_sound":  1,       # (handle) -> null
    "pygame_stop_sound":  1,       # (handle) -> null
    "pygame_set_volume":  2,       # (handle, volume 0.0-1.0) -> null
}


class CRTError(Exception):
    """Base class for user-facing CRT errors with an optional source line."""
    def __init__(self, message, line=None):
        self.message = message
        self.line = line
        super().__init__(self.format())

    def format(self):
        if self.line is not None:
            return f"line {self.line}: {self.message}"
        return self.message


class LexError(CRTError):
    pass


class ParseError(CRTError):
    pass


class CompileError(CRTError):
    pass


class CRTRuntimeError(CRTError):
    pass


# =====================================================================
# 2. LEXER
# =====================================================================

TOKEN_TYPES = [
    ("COMMENT_BLOCK", r'/\*.*?\*/'),
    ("COMMENT_LINE",  r'//[^\n]*'),
    ("NEWLINE",  r'\n'),
    ("NUMBER",   r'\d+\.\d+|\d+'),
    ("STRING",   r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\''),
    ("KEYWORD",  r'\b(let|if|else|while|for|fn|return|print|true|false|null|and|or|not|break|continue)\b'),
    ("IDENT",    r'[a-zA-Z_][a-zA-Z0-9_]*'),
    ("OP",       r'==|!=|<=|>=|&&|\|\||[+\-*/%=<>!]'),
    ("LPAREN",   r'\('),
    ("RPAREN",   r'\)'),
    ("LBRACE",   r'\{'),
    ("RBRACE",   r'\}'),
    ("LBRACK",   r'\['),
    ("RBRACK",   r'\]'),
    ("COLON",    r':'),
    ("COMMA",    r','),
    ("SEMI",     r';'),
    ("DOT",      r'\.'),
    ("SKIP",     r'[ \t\r]+'),
    ("MISMATCH", r'.'),
]

_TOKEN_RE = re.compile(
    '|'.join(f'(?P<{name}>{pattern})' for name, pattern in TOKEN_TYPES),
    re.DOTALL,
)

_ESCAPES = {'n': '\n', 't': '\t', 'r': '\r', '"': '"', "'": "'", '\\': '\\', '0': '\0'}


def _unescape(s):
    out = []
    i = 0
    while i < len(s):
        c = s[i]
        if c == '\\' and i + 1 < len(s):
            out.append(_ESCAPES.get(s[i + 1], s[i + 1]))
            i += 2
        else:
            out.append(c)
            i += 1
    return ''.join(out)


class Token:
    __slots__ = ("type", "value", "line")

    def __init__(self, type_, value, line):
        self.type = type_
        self.value = value
        self.line = line

    def __repr__(self):
        return f"Token({self.type}, {self.value!r}, line={self.line})"


def tokenize(code):
    tokens = []
    line = 1
    pos = 0
    length = len(code)
    while pos < length:
        m = _TOKEN_RE.match(code, pos)
        if not m:
            raise LexError(f"Unexpected character: {code[pos]!r}", line)
        kind = m.lastgroup
        value = m.group()
        pos = m.end()

        if kind == 'NEWLINE':
            line += 1
            continue
        elif kind in ('SKIP', 'COMMENT_LINE'):
            continue
        elif kind == 'COMMENT_BLOCK':
            line += value.count('\n')
            continue
        elif kind == 'MISMATCH':
            raise LexError(f"Unexpected character: {value!r}", line)
        elif kind == 'STRING':
            value = _unescape(value[1:-1])
        tokens.append(Token(kind, value, line))
    tokens.append(Token('EOF', None, line))
    return tokens


# =====================================================================
# AST NODES
# =====================================================================

class ASTNode:
    line = None


class ProgramNode(ASTNode):
    def __init__(self, stmts):
        self.stmts = stmts


class VarDeclNode(ASTNode):
    def __init__(self, name, val, line=None):
        self.name, self.val, self.line = name, val, line


class AssignNode(ASTNode):
    def __init__(self, target, val, line=None):
        self.target, self.val, self.line = target, val, line


class PrintNode(ASTNode):
    def __init__(self, expr, line=None):
        self.expr, self.line = expr, line


class IfNode(ASTNode):
    def __init__(self, branches, else_body, line=None):
        self.branches, self.else_body, self.line = branches, else_body, line


class WhileNode(ASTNode):
    def __init__(self, cond, body, line=None):
        self.cond, self.body, self.line = cond, body, line


class ForNode(ASTNode):
    def __init__(self, init, cond, update, body, line=None):
        self.init, self.cond, self.update, self.body, self.line = init, cond, update, body, line


class BreakNode(ASTNode):
    def __init__(self, line=None):
        self.line = line


class ContinueNode(ASTNode):
    def __init__(self, line=None):
        self.line = line


class FuncDeclNode(ASTNode):
    def __init__(self, name, params, body, line=None):
        self.name, self.params, self.body, self.line = name, params, body, line


class ReturnNode(ASTNode):
    def __init__(self, expr, line=None):
        self.expr, self.line = expr, line


class CallNode(ASTNode):
    def __init__(self, callee, args, line=None):
        self.callee, self.args, self.line = callee, args, line


class UnaryOpNode(ASTNode):
    def __init__(self, op, expr, line=None):
        self.op, self.expr, self.line = op, expr, line


class BinaryOpNode(ASTNode):
    def __init__(self, left, op, right, line=None):
        self.left, self.op, self.right, self.line = left, op, right, line


class LiteralNode(ASTNode):
    def __init__(self, val, line=None):
        self.val, self.line = val, line


class VarAccessNode(ASTNode):
    def __init__(self, name, line=None):
        self.name, self.line = name, line


class ListNode(ASTNode):
    def __init__(self, elems, line=None):
        self.elems, self.line = elems, line


class ObjectNode(ASTNode):
    def __init__(self, pairs, line=None):
        self.pairs, self.line = pairs, line


class MemberAccessNode(ASTNode):
    def __init__(self, obj, member, line=None):
        self.obj, self.member, self.line = obj, member, line


class IndexAccessNode(ASTNode):
    def __init__(self, obj, index, line=None):
        self.obj, self.index, self.line = obj, index, line


# =====================================================================
# PARSER (recursive descent, precedence climbing for expressions)
# =====================================================================

class Parser:
    def __init__(self, tokens):
        self.tokens = tokens
        self.pos = 0

    def peek(self, offset=0):
        idx = self.pos + offset
        return self.tokens[idx] if idx < len(self.tokens) else self.tokens[-1]

    def at_end(self):
        return self.peek().type == 'EOF'

    def consume(self, expected_type=None, expected_value=None):
        tok = self.peek()
        if tok.type == 'EOF' and expected_type != 'EOF':
            raise ParseError("Unexpected end of input", tok.line)
        if expected_type and tok.type != expected_type:
            raise ParseError(f"Expected {expected_type}, got {tok.type} ({tok.value!r})", tok.line)
        if expected_value and tok.value != expected_value:
            raise ParseError(f"Expected '{expected_value}', got '{tok.value}'", tok.line)
        self.pos += 1
        return tok

    def match_kw(self, *values):
        tok = self.peek()
        return tok.type == 'KEYWORD' and tok.value in values

    def match_op(self, *values):
        tok = self.peek()
        return tok.type == 'OP' and tok.value in values

    def opt_semi(self):
        if self.peek().type == 'SEMI':
            self.consume('SEMI')

    # ---- top level -----------------------------------------------------

    def parse(self):
        stmts = []
        while not self.at_end():
            stmts.append(self.parse_stmt())
        return ProgramNode(stmts)

    def parse_block(self):
        self.consume('LBRACE')
        body = []
        while not self.at_end() and self.peek().type != 'RBRACE':
            body.append(self.parse_stmt())
        self.consume('RBRACE')
        return body

    def parse_stmt(self):
        if self.match_kw('let'):
            return self.parse_var_decl()
        if self.match_kw('print'):
            return self.parse_print()
        if self.match_kw('if'):
            return self.parse_if()
        if self.match_kw('while'):
            return self.parse_while()
        if self.match_kw('for'):
            return self.parse_for()
        if self.match_kw('fn'):
            return self.parse_fn_decl()
        if self.match_kw('return'):
            return self.parse_return()
        if self.match_kw('break'):
            tok = self.consume('KEYWORD', 'break')
            self.opt_semi()
            return BreakNode(tok.line)
        if self.match_kw('continue'):
            tok = self.consume('KEYWORD', 'continue')
            self.opt_semi()
            return ContinueNode(tok.line)
        expr = self.parse_expr()
        self.opt_semi()
        return expr

    def parse_var_decl(self):
        line = self.peek().line
        self.consume('KEYWORD', 'let')
        name = self.consume('IDENT').value
        self.consume('OP', '=')
        val = self.parse_expr()
        self.opt_semi()
        return VarDeclNode(name, val, line)

    def parse_print(self):
        line = self.peek().line
        self.consume('KEYWORD', 'print')
        self.consume('LPAREN')
        val = self.parse_expr()
        self.consume('RPAREN')
        self.opt_semi()
        return PrintNode(val, line)

    def parse_if(self):
        line = self.peek().line
        self.consume('KEYWORD', 'if')
        self.consume('LPAREN')
        cond = self.parse_expr()
        self.consume('RPAREN')
        body = self.parse_block()
        branches = [(cond, body)]
        else_body = None
        while self.match_kw('else'):
            self.consume('KEYWORD', 'else')
            if self.match_kw('if'):
                self.consume('KEYWORD', 'if')
                self.consume('LPAREN')
                c2 = self.parse_expr()
                self.consume('RPAREN')
                branches.append((c2, self.parse_block()))
            else:
                else_body = self.parse_block()
                break
        self.opt_semi()
        return IfNode(branches, else_body, line)

    def parse_while(self):
        line = self.peek().line
        self.consume('KEYWORD', 'while')
        self.consume('LPAREN')
        cond = self.parse_expr()
        self.consume('RPAREN')
        body = self.parse_block()
        self.opt_semi()
        return WhileNode(cond, body, line)

    def parse_for(self):
        line = self.peek().line
        self.consume('KEYWORD', 'for')
        self.consume('LPAREN')
        init = None
        if self.peek().type != 'SEMI':
            init = self.parse_var_decl() if self.match_kw('let') else self.parse_expr()
        else:
            self.consume('SEMI')
        if not isinstance(init, VarDeclNode):
            pass
        # after parse_var_decl or bare expr statement, semicolon already
        # consumed by opt_semi() inside those helpers only for statements;
        # parse_expr() alone does not consume it, so handle explicitly:
        if self.peek().type == 'SEMI':
            self.consume('SEMI')
        cond = None
        if self.peek().type != 'SEMI':
            cond = self.parse_expr()
        self.consume('SEMI')
        update = None
        if self.peek().type != 'RPAREN':
            update = self.parse_expr()
        self.consume('RPAREN')
        body = self.parse_block()
        self.opt_semi()
        return ForNode(init, cond, update, body, line)

    def parse_fn_decl(self):
        line = self.peek().line
        self.consume('KEYWORD', 'fn')
        name = self.consume('IDENT').value
        self.consume('LPAREN')
        params = []
        if self.peek().type != 'RPAREN':
            params.append(self.consume('IDENT').value)
            while self.peek().type == 'COMMA':
                self.consume('COMMA')
                params.append(self.consume('IDENT').value)
        self.consume('RPAREN')
        body = self.parse_block()
        self.opt_semi()
        return FuncDeclNode(name, params, body, line)

    def parse_return(self):
        line = self.peek().line
        self.consume('KEYWORD', 'return')
        expr = None
        if self.peek().type not in ('SEMI', 'RBRACE') and not self.at_end():
            expr = self.parse_expr()
        self.opt_semi()
        return ReturnNode(expr, line)

    # ---- expressions (lowest to highest precedence) --------------------

    def parse_expr(self):
        return self.parse_assignment()

    def parse_assignment(self):
        left = self.parse_or()
        if self.match_op('='):
            line = self.peek().line
            self.consume('OP', '=')
            if not isinstance(left, (VarAccessNode, MemberAccessNode, IndexAccessNode)):
                raise ParseError("Invalid assignment target", line)
            return AssignNode(left, self.parse_assignment(), line)
        return left

    def parse_or(self):
        left = self.parse_and()
        while self.match_op('||') or self.match_kw('or'):
            line = self.peek().line
            self.consume()
            left = BinaryOpNode(left, '||', self.parse_and(), line)
        return left

    def parse_and(self):
        left = self.parse_equality()
        while self.match_op('&&') or self.match_kw('and'):
            line = self.peek().line
            self.consume()
            left = BinaryOpNode(left, '&&', self.parse_equality(), line)
        return left

    def parse_equality(self):
        left = self.parse_comparison()
        while self.match_op('==', '!='):
            line = self.peek().line
            op = self.consume('OP').value
            left = BinaryOpNode(left, op, self.parse_comparison(), line)
        return left

    def parse_comparison(self):
        left = self.parse_additive()
        while self.match_op('<', '>', '<=', '>='):
            line = self.peek().line
            op = self.consume('OP').value
            left = BinaryOpNode(left, op, self.parse_additive(), line)
        return left

    def parse_additive(self):
        left = self.parse_multiplicative()
        while self.match_op('+', '-'):
            line = self.peek().line
            op = self.consume('OP').value
            left = BinaryOpNode(left, op, self.parse_multiplicative(), line)
        return left

    def parse_multiplicative(self):
        left = self.parse_unary()
        while self.match_op('*', '/', '%'):
            line = self.peek().line
            op = self.consume('OP').value
            left = BinaryOpNode(left, op, self.parse_unary(), line)
        return left

    def parse_unary(self):
        if self.match_op('-'):
            line = self.peek().line
            self.consume('OP', '-')
            return UnaryOpNode('-', self.parse_unary(), line)
        if self.match_op('!') or self.match_kw('not'):
            line = self.peek().line
            self.consume()
            return UnaryOpNode('!', self.parse_unary(), line)
        return self.parse_postfix()

    def parse_postfix(self):
        expr = self.parse_primary()
        while True:
            tok = self.peek()
            if tok.type == 'DOT':
                self.consume('DOT')
                member = self.consume('IDENT').value
                expr = MemberAccessNode(expr, member, tok.line)
            elif tok.type == 'LBRACK':
                self.consume('LBRACK')
                idx = self.parse_expr()
                self.consume('RBRACK')
                expr = IndexAccessNode(expr, idx, tok.line)
            elif tok.type == 'LPAREN':
                self.consume('LPAREN')
                args = []
                if self.peek().type != 'RPAREN':
                    args.append(self.parse_expr())
                    while self.peek().type == 'COMMA':
                        self.consume('COMMA')
                        args.append(self.parse_expr())
                self.consume('RPAREN')
                expr = CallNode(expr, args, tok.line)
            else:
                break
        return expr

    def parse_primary(self):
        tok = self.peek()
        if tok.type == 'NUMBER':
            self.consume()
            return LiteralNode(float(tok.value) if '.' in tok.value else int(tok.value), tok.line)
        if tok.type == 'STRING':
            self.consume()
            return LiteralNode(tok.value, tok.line)
        if self.match_kw('true', 'false'):
            self.consume()
            return LiteralNode(tok.value == 'true', tok.line)
        if self.match_kw('null'):
            self.consume()
            return LiteralNode(None, tok.line)
        if tok.type == 'IDENT':
            self.consume()
            return VarAccessNode(tok.value, tok.line)
        if tok.type == 'LPAREN':
            self.consume('LPAREN')
            e = self.parse_expr()
            self.consume('RPAREN')
            return e
        if tok.type == 'LBRACK':
            self.consume('LBRACK')
            elems = []
            if self.peek().type != 'RBRACK':
                elems.append(self.parse_expr())
                while self.peek().type == 'COMMA':
                    self.consume('COMMA')
                    elems.append(self.parse_expr())
            self.consume('RBRACK')
            return ListNode(elems, tok.line)
        if tok.type == 'LBRACE':
            self.consume('LBRACE')
            pairs = []
            if self.peek().type != 'RBRACE':
                k = self._parse_obj_key()
                self.consume('COLON')
                pairs.append((k, self.parse_expr()))
                while self.peek().type == 'COMMA':
                    self.consume('COMMA')
                    k = self._parse_obj_key()
                    self.consume('COLON')
                    pairs.append((k, self.parse_expr()))
            self.consume('RBRACE')
            return ObjectNode(pairs, tok.line)
        raise ParseError(f"Unexpected token: {tok.type} ({tok.value!r})", tok.line)

    def _parse_obj_key(self):
        tok = self.peek()
        if tok.type in ('IDENT', 'KEYWORD'):
            self.consume()
            return tok.value
        if tok.type == 'STRING':
            self.consume()
            return tok.value
        raise ParseError(f"Expected object key, got {tok.type}", tok.line)


# =====================================================================
# 3. COMPILER (AST -> CVM bytecode)
# =====================================================================

BIN_OPS = {
    '+': 'ADD', '-': 'SUB', '*': 'MUL', '/': 'DIV', '%': 'MOD',
    '==': 'EQ', '!=': 'NEQ', '<': 'LT', '>': 'GT', '<=': 'LTE', '>=': 'GTE',
}


class Instr:
    __slots__ = ("op", "arg_type", "arg_val", "extra", "line")

    def __init__(self, op, arg_type, arg_val, extra, line):
        self.op, self.arg_type, self.arg_val, self.extra, self.line = op, arg_type, arg_val, extra, line


class FuncMeta:
    def __init__(self, name, params, addr):
        self.name, self.params, self.addr = name, params, addr


class LoopCtx:
    """Tracks patch points for break/continue inside one loop."""
    def __init__(self, continue_target=None):
        self.break_jumps = []
        self.continue_jumps = []
        self.continue_target = continue_target  # set immediately if known


class BinaryCompiler:
    def __init__(self):
        self.instructions = []
        self.strings = []
        self._string_index = {}
        self.loop_stack = []
        self.functions = {}
        self.current_func_params = None  # set of param names while compiling a fn body

    def add_string(self, s):
        if s in self._string_index:
            return self._string_index[s]
        idx = len(self.strings)
        self.strings.append(s)
        self._string_index[s] = idx
        return idx

    def emit(self, opcode_name, arg_type=ARG_NONE, arg_val=0, extra=0, line=None):
        idx = len(self.instructions)
        self.instructions.append(Instr(OPCODES[opcode_name], arg_type, arg_val, extra, line))
        return idx

    def here(self):
        return len(self.instructions)

    def patch(self, idx, target):
        self.instructions[idx].arg_val = target

    # ---- program structure ---------------------------------------------

    def compile_program(self, node):
        fn_decls = [s for s in node.stmts if isinstance(s, FuncDeclNode)]
        top_stmts = [s for s in node.stmts if not isinstance(s, FuncDeclNode)]

        seen = set()
        for fn in fn_decls:
            if fn.name in seen:
                raise CompileError(f"function '{fn.name}' declared more than once", fn.line)
            seen.add(fn.name)

        skip_idx = self.emit("JUMP", ARG_IMM, 0) if fn_decls else None

        # Pre-register addresses with a placeholder pass so functions can
        # call each other and themselves (recursion) regardless of order.
        # Register each name in the string table now (even if the function
        # is never called) so assemble() can always find it when writing
        # the function table.
        for fn in fn_decls:
            self.functions[fn.name] = FuncMeta(fn.name, fn.params, None)
            self.add_string(fn.name)
        for fn in fn_decls:
            self.functions[fn.name].addr = self.here()
            self._compile_function_body(fn)

        if skip_idx is not None:
            self.patch(skip_idx, self.here())

        for s in top_stmts:
            self.compile(s)
        self.emit("HALT")

    def _compile_function_body(self, fn):
        self.emit("MAKE_FRAME", ARG_IMM, len(fn.params), line=fn.line)
        for p in reversed(fn.params):
            s_idx = self.add_string(p)
            self.emit("STORE_LOCAL", ARG_STR, s_idx, line=fn.line)
        for s in fn.body:
            self.compile(s)
        self.emit("PUSH_NULL", line=fn.line)
        self.emit("RET", line=fn.line)

    # ---- statements / expressions ---------------------------------------

    def compile(self, node):
        if isinstance(node, LiteralNode):
            self._lit(node)
        elif isinstance(node, VarAccessNode):
            self.emit("LOAD", ARG_STR, self.add_string(node.name), line=node.line)
        elif isinstance(node, VarDeclNode):
            self.compile(node.val)
            self.emit("STORE_LOCAL", ARG_STR, self.add_string(node.name), line=node.line)
        elif isinstance(node, AssignNode):
            self._assign(node)
        elif isinstance(node, UnaryOpNode):
            self.compile(node.expr)
            self.emit("NEG" if node.op == '-' else "NOT", line=node.line)
        elif isinstance(node, BinaryOpNode):
            self._binop(node)
        elif isinstance(node, PrintNode):
            self.compile(node.expr)
            self.emit("PRINT", line=node.line)
        elif isinstance(node, IfNode):
            self._if(node)
        elif isinstance(node, WhileNode):
            self._while(node)
        elif isinstance(node, ForNode):
            self._for(node)
        elif isinstance(node, BreakNode):
            if not self.loop_stack:
                raise CompileError("'break' outside of a loop", node.line)
            idx = self.emit("JUMP", ARG_IMM, 0, line=node.line)
            self.loop_stack[-1].break_jumps.append(idx)
        elif isinstance(node, ContinueNode):
            if not self.loop_stack:
                raise CompileError("'continue' outside of a loop", node.line)
            ctx = self.loop_stack[-1]
            if ctx.continue_target is not None:
                self.emit("JUMP", ARG_IMM, ctx.continue_target, line=node.line)
            else:
                idx = self.emit("JUMP", ARG_IMM, 0, line=node.line)
                ctx.continue_jumps.append(idx)
        elif isinstance(node, FuncDeclNode):
            raise CompileError("functions can only be declared at the top level", node.line)
        elif isinstance(node, ReturnNode):
            self.compile(node.expr) if node.expr is not None else self.emit("PUSH_NULL", line=node.line)
            self.emit("RET", line=node.line)
        elif isinstance(node, CallNode):
            self._call(node)
        elif isinstance(node, ListNode):
            for e in node.elems:
                self.compile(e)
            self.emit("MAKE_LIST", ARG_IMM, len(node.elems), line=node.line)
        elif isinstance(node, ObjectNode):
            for k, v in node.pairs:
                self.emit("PUSH_STR", ARG_STR, self.add_string(k), line=node.line)
                self.compile(v)
            self.emit("MAKE_OBJECT", ARG_IMM, len(node.pairs), line=node.line)
        elif isinstance(node, MemberAccessNode):
            self.compile(node.obj)
            self.emit("GET_MEMBER", ARG_STR, self.add_string(node.member), line=node.line)
        elif isinstance(node, IndexAccessNode):
            self.compile(node.obj)
            self.compile(node.index)
            self.emit("GET_INDEX", line=node.line)
        else:
            raise CompileError(f"Cannot compile node: {type(node).__name__}", node.line)

    def _lit(self, node):
        v = node.val
        if isinstance(v, bool):
            self.emit("PUSH_BOOL", ARG_IMM, 1 if v else 0, line=node.line)
        elif v is None:
            self.emit("PUSH_NULL", line=node.line)
        elif isinstance(v, int):
            self.emit("PUSH_INT", ARG_IMM, v, line=node.line)
        elif isinstance(v, float):
            self.emit("PUSH_FLOAT", ARG_STR, self.add_string(repr(v)), line=node.line)
        elif isinstance(v, str):
            self.emit("PUSH_STR", ARG_STR, self.add_string(v), line=node.line)

    def _assign(self, node):
        target = node.target
        if isinstance(target, VarAccessNode):
            self.compile(node.val)
            self.emit("STORE", ARG_STR, self.add_string(target.name), line=node.line)
        elif isinstance(target, MemberAccessNode):
            self.compile(target.obj)
            self.compile(node.val)
            self.emit("SET_MEMBER", ARG_STR, self.add_string(target.member), line=node.line)
        elif isinstance(target, IndexAccessNode):
            self.compile(target.obj)
            self.compile(target.index)
            self.compile(node.val)
            self.emit("SET_INDEX", line=node.line)
        else:
            raise CompileError("Invalid assignment target", node.line)

    def _binop(self, node):
        if node.op == '&&':
            self.compile(node.left)
            self.emit("DUP", line=node.line)
            jf = self.emit("JUMP_IF_FALSE", ARG_IMM, 0, line=node.line)
            self.emit("POP", line=node.line)
            self.compile(node.right)
            self.patch(jf, self.here())
            return
        if node.op == '||':
            self.compile(node.left)
            self.emit("DUP", line=node.line)
            jt = self.emit("JUMP_IF_TRUE", ARG_IMM, 0, line=node.line)
            self.emit("POP", line=node.line)
            self.compile(node.right)
            self.patch(jt, self.here())
            return
        self.compile(node.left)
        self.compile(node.right)
        self.emit(BIN_OPS[node.op], line=node.line)

    def _if(self, node):
        end_jumps = []
        n = len(node.branches)
        for i, (cond, body) in enumerate(node.branches):
            self.compile(cond)
            jf = self.emit("JUMP_IF_FALSE", ARG_IMM, 0, line=cond.line)
            for s in body:
                self.compile(s)
            if node.else_body is not None or i < n - 1:
                end_jumps.append(self.emit("JUMP", ARG_IMM, 0, line=node.line))
            self.patch(jf, self.here())
        if node.else_body is not None:
            for s in node.else_body:
                self.compile(s)
        for j in end_jumps:
            self.patch(j, self.here())

    def _while(self, node):
        loop_start = self.here()
        self.compile(node.cond)
        jf = self.emit("JUMP_IF_FALSE", ARG_IMM, 0, line=node.line)
        ctx = LoopCtx(continue_target=loop_start)
        self.loop_stack.append(ctx)
        for s in node.body:
            self.compile(s)
        self.emit("JUMP", ARG_IMM, loop_start, line=node.line)
        end = self.here()
        self.patch(jf, end)
        self.loop_stack.pop()
        for b in ctx.break_jumps:
            self.patch(b, end)

    def _for(self, node):
        if node.init is not None:
            self.compile(node.init)
        loop_start = self.here()
        jf = None
        if node.cond is not None:
            self.compile(node.cond)
            jf = self.emit("JUMP_IF_FALSE", ARG_IMM, 0, line=node.line)
        # continue must run the update step before re-checking the condition,
        # so continue jumps are patched to the update address once known.
        ctx = LoopCtx(continue_target=None)
        self.loop_stack.append(ctx)
        for s in node.body:
            self.compile(s)
        update_addr = self.here()
        if node.update is not None:
            self.compile(node.update)
        self.emit("JUMP", ARG_IMM, loop_start, line=node.line)
        end = self.here()
        if jf is not None:
            self.patch(jf, end)
        self.loop_stack.pop()
        for c in ctx.continue_jumps:
            self.patch(c, update_addr)
        for b in ctx.break_jumps:
            self.patch(b, end)

    def _call(self, node):
        if not isinstance(node.callee, VarAccessNode):
            raise CompileError("Only named functions can be called", node.line)
        name = node.callee.name
        if name in BUILTINS:
            for a in node.args:
                self.compile(a)
            self.emit("CALL_BUILTIN", ARG_STR, self.add_string(name),
                      extra=len(node.args), line=node.line)
            return
        if name in GFX_BUILTINS:
            expected = GFX_BUILTINS[name]
            allowed = expected if isinstance(expected, tuple) else (expected,)
            if len(node.args) not in allowed:
                choices = " or ".join(str(n) for n in allowed)
                raise CompileError(
                    f"'{name}' takes {choices} argument(s), got {len(node.args)}", node.line)
            for a in node.args:
                self.compile(a)
            self.emit("CALL_GFX", ARG_STR, self.add_string(name),
                      extra=len(node.args), line=node.line)
            return
        for a in node.args:
            self.compile(a)
        self.emit("CALL", ARG_STR, self.add_string(name),
                  extra=len(node.args), line=node.line)

    def assemble(self):
        out = bytearray(b"CVM\x04")
        out.extend(struct.pack(">H", len(self.strings)))
        for s in self.strings:
            enc = s.encode('utf-8')
            out.extend(struct.pack(">H", len(enc)))
            out.extend(enc)
        out.extend(struct.pack(">I", len(self.instructions)))
        for ins in self.instructions:
            out.extend(struct.pack(">BBih", ins.op, ins.arg_type, ins.arg_val, ins.extra))
        # Function table: the instruction stream alone doesn't record which
        # name owns which MAKE_FRAME address (that mapping only exists in
        # the compiler's `functions` dict), so persist it here as
        # (name string-index, entry address, param count) triples. This
        # lets the loader resolve CALL targets by name instead of guessing
        # from call order (which breaks for self-recursive functions), and
        # lets the runtime give a clear "wrong number of arguments" error
        # instead of a raw stack underflow.
        out.extend(struct.pack(">H", len(self.functions)))
        for meta in self.functions.values():
            out.extend(struct.pack(">HIH", self._string_index[meta.name], meta.addr, len(meta.params)))
        # Line-number table: one i32 per instruction (-1 = unknown), so
        # runtime errors loaded from a .cvm file can still report a source
        # line instead of always saying "line: unknown". Run-length encoded
        # as (count, line) pairs since runs of same-line instructions are
        # common (e.g. a whole expression compiles to several instructions
        # that all share one line).
        runs = []
        for ins in self.instructions:
            line = ins.line if ins.line is not None else -1
            if runs and runs[-1][1] == line:
                runs[-1][0] += 1
            else:
                runs.append([1, line])
        out.extend(struct.pack(">I", len(runs)))
        for count, line in runs:
            out.extend(struct.pack(">Ii", count, line))
        return bytes(out)


def compile_source(code):
    """Convenience: source text -> assembled CVM bytecode bytes."""
    tokens = tokenize(code)
    ast = Parser(tokens).parse()
    compiler = BinaryCompiler()
    # resolve forward function calls by pre-scanning names (handled inside
    # compile_program via two passes), then verify all called names exist.
    compiler.compile_program(ast)
    _verify_calls(ast, compiler)
    return compiler.assemble()


def _verify_calls(ast, compiler):
    """Walk the AST and raise a clear CompileError for calls to unknown
    functions (that aren't built-ins), instead of failing at runtime."""
    known = set(compiler.functions.keys())

    def walk(node):
        if isinstance(node, ProgramNode):
            for s in node.stmts:
                walk(s)
        elif isinstance(node, CallNode):
            if isinstance(node.callee, VarAccessNode):
                nm = node.callee.name
                if nm not in BUILTINS and nm not in GFX_BUILTINS and nm not in known:
                    raise CompileError(f"call to undefined function '{nm}'", node.line)
            for a in node.args:
                walk(a)
        elif isinstance(node, FuncDeclNode):
            for s in node.body:
                walk(s)
        elif isinstance(node, (VarDeclNode,)):
            walk(node.val)
        elif isinstance(node, AssignNode):
            walk(node.target)
            walk(node.val)
        elif isinstance(node, PrintNode):
            walk(node.expr)
        elif isinstance(node, IfNode):
            for c, b in node.branches:
                walk(c)
                for s in b:
                    walk(s)
            if node.else_body:
                for s in node.else_body:
                    walk(s)
        elif isinstance(node, WhileNode):
            walk(node.cond)
            for s in node.body:
                walk(s)
        elif isinstance(node, ForNode):
            if node.init: walk(node.init)
            if node.cond: walk(node.cond)
            if node.update: walk(node.update)
            for s in node.body:
                walk(s)
        elif isinstance(node, ReturnNode):
            if node.expr: walk(node.expr)
        elif isinstance(node, (UnaryOpNode,)):
            walk(node.expr)
        elif isinstance(node, BinaryOpNode):
            walk(node.left)
            walk(node.right)
        elif isinstance(node, ListNode):
            for e in node.elems:
                walk(e)
        elif isinstance(node, ObjectNode):
            for k, v in node.pairs:
                walk(v)
        elif isinstance(node, MemberAccessNode):
            walk(node.obj)
        elif isinstance(node, IndexAccessNode):
            walk(node.obj)
            walk(node.index)
        # LiteralNode, VarAccessNode, BreakNode, ContinueNode: nothing to check

    walk(ast)


# =====================================================================
# 3b. ASSEMBLER (CRT assembly text -> CVM bytecode)
# =====================================================================
#
# A human-writable text format for CVM bytecode, for people who want to
# hand-write or generate bytecode directly instead of going through the
# CRT language front-end. Mirrors what `crt disasm` prints closely enough
# that a disassembly is a short edit away from valid input.
#
# SYNTAX
#   ; a comment, to end of line
#   label:                       define a label (address of the *next*
#                                 instruction) -- used as a jump target
#   .func name arity             mark the next instruction's address as
#                                 the entry point of function `name`,
#                                 taking `arity` parameters
#   MNEMONIC                     instruction with no operand
#   MNEMONIC 42                  instruction with an integer operand
#   MNEMONIC "text"               instruction with a string operand
#                                  (interned into the string table)
#   MNEMONIC 3.5                   PUSH_FLOAT's operand is written as a
#                                    float literal, stored the same way
#                                    the compiler stores one (as a string)
#   MNEMONIC label                 a label used as a jump target, e.g.
#                                    JUMP loop_start
#   MNEMONIC "name", 2              string operand plus an extra field
#                                    (argc), e.g. CALL "add", 2
#
# Any bare identifier operand that isn't a known label at assemble time
# is an error (undefined label), so typos are caught rather than silently
# assembled as address 0.

class AsmError(CRTError):
    pass


_ASM_TOKEN_TYPES = [
    ("COMMENT",  r';[^\n]*'),
    ("NEWLINE",  r'\n'),
    ("STRING",   r'"(?:\\.|[^"\\])*"'),
    ("FLOAT",    r'-?\d+\.\d+'),
    ("INT",      r'-?\d+'),
    ("DIRECTIVE", r'\.[a-zA-Z_][a-zA-Z0-9_]*'),
    ("LABELDEF", r'[a-zA-Z_][a-zA-Z0-9_]*:'),
    ("IDENT",    r'[a-zA-Z_][a-zA-Z0-9_]*'),
    ("COMMA",    r','),
    ("SKIP",     r'[ \t\r]+'),
    ("MISMATCH", r'.'),
]

_ASM_TOKEN_RE = re.compile(
    '|'.join(f'(?P<{name}>{pattern})' for name, pattern in _ASM_TOKEN_TYPES),
    re.DOTALL,
)


class AsmToken:
    __slots__ = ("type", "value", "line")

    def __init__(self, type_, value, line):
        self.type, self.value, self.line = type_, value, line


def _asm_tokenize(text):
    tokens = []
    line = 1
    pos = 0
    length = len(text)
    while pos < length:
        m = _ASM_TOKEN_RE.match(text, pos)
        if not m:
            raise AsmError(f"unexpected character: {text[pos]!r}", line)
        kind = m.lastgroup
        value = m.group()
        pos = m.end()
        if kind == 'NEWLINE':
            tokens.append(AsmToken('NEWLINE', None, line))
            line += 1
            continue
        if kind in ('SKIP', 'COMMENT'):
            continue
        if kind == 'MISMATCH':
            raise AsmError(f"unexpected character: {value!r}", line)
        if kind == 'STRING':
            value = _unescape(value[1:-1])
        elif kind == 'LABELDEF':
            value = value[:-1]
        tokens.append(AsmToken(kind, value, line))
    tokens.append(AsmToken('EOF', None, line))
    return tokens


class Assembler:
    """Parses CRT assembly text and produces the same kind of assembled
    bytes BinaryCompiler.assemble() does, so the output is a drop-in
    .cvm file usable by `crt exec` / `crt disasm` / the REPL loader."""

    def __init__(self):
        self.instructions = []   # list of Instr
        self.strings = []
        self._string_index = {}
        self.functions = {}      # name -> FuncMeta
        self._pending_func = None  # (name, arity) waiting for the next instruction's address
        self._labels = {}          # name -> resolved address (once seen as a def)
        self._label_uses = []      # (instr_index, label_name, line) needing patching

    def add_string(self, s):
        if s in self._string_index:
            return self._string_index[s]
        idx = len(self.strings)
        self.strings.append(s)
        self._string_index[s] = idx
        return idx

    def here(self):
        return len(self.instructions)

    def assemble_text(self, text):
        tokens = _asm_tokenize(text)
        pos = 0

        def peek(off=0):
            i = pos + off
            return tokens[i] if i < len(tokens) else tokens[-1]

        while True:
            tok = peek()
            if tok.type == 'EOF':
                break
            if tok.type == 'NEWLINE':
                pos += 1
                continue
            if tok.type == 'LABELDEF':
                name = tok.value
                if name in self._labels:
                    raise AsmError(f"label '{name}' defined more than once", tok.line)
                self._labels[name] = self.here()
                if self._pending_func is not None:
                    fname, arity = self._pending_func
                    self.functions[fname] = FuncMeta(fname, [None] * arity, self.here())
                    self.add_string(fname)
                    self._pending_func = None
                pos += 1
                continue
            if tok.type == 'DIRECTIVE':
                pos = self._parse_directive(tokens, pos)
                continue
            if tok.type == 'IDENT':
                pos = self._parse_instruction(tokens, pos)
                continue
            raise AsmError(f"unexpected token {tok.type} ({tok.value!r})", tok.line)

        if self._pending_func is not None:
            fname, arity = self._pending_func
            self.functions[fname] = FuncMeta(fname, [None] * arity, self.here())
            self.add_string(fname)

        # Resolve label references now that every label has been seen.
        for instr_idx, label_name, line in self._label_uses:
            if label_name not in self._labels:
                raise AsmError(f"undefined label '{label_name}'", line)
            self.instructions[instr_idx].arg_val = self._labels[label_name]

        return self

    def _parse_directive(self, tokens, pos):
        tok = tokens[pos]
        name = tok.value
        pos += 1
        if name == '.func':
            fname_tok = tokens[pos]
            if fname_tok.type != 'IDENT':
                raise AsmError(".func expects a name", fname_tok.line)
            fname = fname_tok.value
            pos += 1
            arity_tok = tokens[pos]
            if arity_tok.type != 'INT':
                raise AsmError(".func expects an integer arity", arity_tok.line)
            arity = int(arity_tok.value)
            pos += 1
            if fname in self.functions or (self._pending_func and self._pending_func[0] == fname):
                raise AsmError(f"function '{fname}' declared more than once", tok.line)
            self._pending_func = (fname, arity)
            return self._expect_line_end(tokens, pos)
        raise AsmError(f"unknown directive '{name}'", tok.line)

    def _expect_line_end(self, tokens, pos):
        tok = tokens[pos]
        if tok.type not in ('NEWLINE', 'EOF'):
            raise AsmError(f"unexpected token after statement: {tok.type} ({tok.value!r})", tok.line)
        if tok.type == 'NEWLINE':
            pos += 1
        return pos

    def _parse_instruction(self, tokens, pos):
        mnem_tok = tokens[pos]
        mnem = mnem_tok.value.upper()
        if mnem not in OPCODES:
            raise AsmError(f"unknown instruction '{mnem_tok.value}'", mnem_tok.line)
        pos += 1

        arg_type, arg_val, extra = ARG_NONE, 0, 0
        tok = tokens[pos]
        if tok.type not in ('NEWLINE', 'EOF'):
            arg_type, arg_val, pos = self._parse_operand(tokens, pos, mnem)
            tok = tokens[pos]
            if tok.type == 'COMMA':
                pos += 1
                extra_tok = tokens[pos]
                if extra_tok.type != 'INT':
                    raise AsmError("expected an integer for the extra field", extra_tok.line)
                extra = int(extra_tok.value)
                pos += 1

        idx = len(self.instructions)
        self.instructions.append(Instr(OPCODES[mnem], arg_type, arg_val, extra, mnem_tok.line))

        if self._pending_func is not None:
            # A .func directive with no intervening label -- the function
            # entry is this instruction itself.
            fname, arity = self._pending_func
            self.functions[fname] = FuncMeta(fname, [None] * arity, idx)
            self.add_string(fname)
            self._pending_func = None

        return self._expect_line_end(tokens, pos)

    def _parse_operand(self, tokens, pos, mnem):
        tok = tokens[pos]
        if tok.type == 'STRING':
            return ARG_STR, self.add_string(tok.value), pos + 1
        if tok.type == 'FLOAT':
            # Stored the same way the compiler stores float literals: as
            # the string form of the float, referenced by string index.
            return ARG_STR, self.add_string(repr(float(tok.value))), pos + 1
        if tok.type == 'INT':
            return ARG_IMM, int(tok.value), pos + 1
        if tok.type == 'IDENT':
            # A bare identifier is a label reference, resolved once every
            # label in the file has been seen.
            idx = len(self.instructions)
            self._label_uses.append((idx, tok.value, tok.line))
            return ARG_IMM, 0, pos + 1
        raise AsmError(f"invalid operand for {mnem}: {tok.type} ({tok.value!r})", tok.line)

    def assemble(self):
        """Produce the same byte layout BinaryCompiler.assemble() does."""
        out = bytearray(b"CVM\x04")
        out.extend(struct.pack(">H", len(self.strings)))
        for s in self.strings:
            enc = s.encode('utf-8')
            out.extend(struct.pack(">H", len(enc)))
            out.extend(enc)
        out.extend(struct.pack(">I", len(self.instructions)))
        for ins in self.instructions:
            out.extend(struct.pack(">BBih", ins.op, ins.arg_type, ins.arg_val, ins.extra))
        out.extend(struct.pack(">H", len(self.functions)))
        for meta in self.functions.values():
            out.extend(struct.pack(">HIH", self._string_index[meta.name], meta.addr, len(meta.params)))
        runs = []
        for ins in self.instructions:
            line = ins.line if ins.line is not None else -1
            if runs and runs[-1][1] == line:
                runs[-1][0] += 1
            else:
                runs.append([1, line])
        out.extend(struct.pack(">I", len(runs)))
        for count, line in runs:
            out.extend(struct.pack(">Ii", count, line))
        return bytes(out)


def assemble_source(text):
    """Convenience: CRT assembly text -> assembled CVM bytecode bytes."""
    return Assembler().assemble_text(text).assemble()


# =====================================================================
# 4. CVM RUNTIME (bytecode -> execution)
# =====================================================================
#
# The runtime is a straightforward stack machine. A CVM module is:
#
#   header   : "CVM" + version byte (currently 0x04)
#   strings  : u16 count, then each string as (u16 length, utf-8 bytes)
#   code     : u32 instruction count, then each instruction as the fixed
#              8-byte record described at the top of this file
#              (>BBih -> opcode u8, arg_type u8, arg_val i32, extra i16)
#   funcs    : u16 count, then each as (u16 name string-index, u32 addr,
#              u16 param count) -- this is how CALL targets get resolved:
#              by name, not by guessing from call order (self-recursive
#              functions can be called before any outside caller reaches
#              them, so order alone can't be trusted), and the param
#              count lets the runtime reject wrong-arity calls cleanly
#              instead of failing with a raw stack underflow.
#
# Values at runtime are plain Python objects: int, float, bool, str, None,
# list, and dict (for CRT objects).


class CVMLoadError(CRTError):
    pass


class Frame:
    """One call frame: local variables plus the return address."""
    __slots__ = ("locals", "return_addr")

    def __init__(self, return_addr):
        self.locals = {}
        self.return_addr = return_addr


class CVMModule:
    """A parsed CVM binary: strings, instructions, and the function table
    (name -> (entry address, param count)) written by assemble()."""

    MAGIC = b"CVM"
    VERSION = 0x04

    def __init__(self, strings, instructions, functions):
        self.strings = strings
        self.instructions = instructions
        self.functions = functions  # name (str) -> (entry address, param count)


def load_module(data):
    """Parse raw CVM bytes into strings, instructions, and the function
    table. Layout (all integers big-endian):
        magic    : "CVM" + version byte
        strings  : u16 count, then each as (u16 length, utf-8 bytes)
        code     : u32 count, then each instruction as (u8 op, u8 arg_type,
                   i32 arg_val, i16 extra)
        funcs    : u16 count, then each as (u16 name string-index, u32 addr,
                   u16 param count)
        lines    : u32 run count, then each as (u32 run length, i32 line);
                   -1 means unknown. Runs cover instructions in order.
    """
    if len(data) < 4 or data[:3] != CVMModule.MAGIC:
        raise CVMLoadError("not a CVM file (bad magic)")
    version = data[3]
    if version != CVMModule.VERSION:
        raise CVMLoadError(f"unsupported CVM version {version} (expected {CVMModule.VERSION})")

    pos = 4
    (str_count,) = struct.unpack_from(">H", data, pos)
    pos += 2
    strings = []
    for _ in range(str_count):
        (slen,) = struct.unpack_from(">H", data, pos)
        pos += 2
        s = data[pos:pos + slen].decode('utf-8')
        pos += slen
        strings.append(s)

    (instr_count,) = struct.unpack_from(">I", data, pos)
    pos += 4
    instructions = []
    for _ in range(instr_count):
        op, arg_type, arg_val, extra = struct.unpack_from(">BBih", data, pos)
        pos += 8
        instructions.append(Instr(op, arg_type, arg_val, extra, None))

    functions = {}
    if pos + 2 <= len(data):
        (func_count,) = struct.unpack_from(">H", data, pos)
        pos += 2
        for _ in range(func_count):
            name_idx, addr, arity = struct.unpack_from(">HIH", data, pos)
            pos += 8
            functions[strings[name_idx]] = (addr, arity)

    if pos + 4 <= len(data):
        (run_count,) = struct.unpack_from(">I", data, pos)
        pos += 4
        idx = 0
        for _ in range(run_count):
            count, line = struct.unpack_from(">Ii", data, pos)
            pos += 8
            resolved = line if line != -1 else None
            for _ in range(count):
                if idx < len(instructions):
                    instructions[idx].line = resolved
                idx += 1

    return CVMModule(strings, instructions, functions)


class _PygameBackend:
    """Thin wrapper around pygame, created lazily the first time a script
    calls any pygame_* builtin. Import is deferred here (not at module load
    time) so `crt run` works fine on machines without pygame installed, as
    long as the script never actually enters graphics mode."""

    _KEY_NAMES = None  # populated on first use, once pygame is importable

    def __init__(self, err_fn):
        self._err = err_fn
        self._pygame = None
        self.screen = None
        self.clock = None
        self._font = None
        self._quit_requested = False
        self._images = {}       # handle (int) -> pygame Surface
        self._sounds = {}       # handle (int) -> pygame Sound
        self._next_handle = 1
        self._mixer_ready = False

    def _ensure_pygame(self):
        if self._pygame is not None:
            return self._pygame
        try:
            import pygame
        except ImportError:
            self._err(
                "pygame is not installed. Install it with: pip install pygame"
            )
        self._pygame = pygame
        if _PygameBackend._KEY_NAMES is None:
            _PygameBackend._KEY_NAMES = {
                "left": pygame.K_LEFT, "right": pygame.K_RIGHT,
                "up": pygame.K_UP, "down": pygame.K_DOWN,
                "space": pygame.K_SPACE, "enter": pygame.K_RETURN,
                "return": pygame.K_RETURN, "escape": pygame.K_ESCAPE,
                "esc": pygame.K_ESCAPE, "shift": pygame.K_LSHIFT,
                "ctrl": pygame.K_LCTRL, "tab": pygame.K_TAB,
            }
            for c in "abcdefghijklmnopqrstuvwxyz":
                _PygameBackend._KEY_NAMES[c] = getattr(pygame, f"K_{c}")
            for d in "0123456789":
                _PygameBackend._KEY_NAMES[d] = getattr(pygame, f"K_{d}")
        return pygame

    def _require_screen(self, caller):
        if self.screen is None:
            self._err(f"{caller}(): call pygame_init() before using graphics")

    def init(self, width, height, title):
        pygame = self._ensure_pygame()
        if width <= 0 or height <= 0:
            self._err("pygame_init(): width and height must be positive")
        pygame.init()
        self.screen = pygame.display.set_mode((width, height))
        pygame.display.set_caption(title)
        self.clock = pygame.time.Clock()
        self._font = pygame.font.SysFont(None, 24)
        self._quit_requested = False

    def quit(self):
        if self._pygame is not None and self.screen is not None:
            self._pygame.quit()
        self.screen = None
        self._images.clear()
        self._sounds.clear()
        self._mixer_ready = False

    def clear(self, color):
        self._require_screen("pygame_clear")
        self.screen.fill(color)

    def rect(self, x, y, w, h, color):
        self._require_screen("pygame_rect")
        self._pygame.draw.rect(self.screen, color, (x, y, w, h))

    def circle(self, x, y, radius, color):
        self._require_screen("pygame_circle")
        if radius < 0:
            self._err("pygame_circle(): radius must be non-negative")
        self._pygame.draw.circle(self.screen, color, (x, y), radius)

    def line(self, x1, y1, x2, y2, color):
        self._require_screen("pygame_line")
        self._pygame.draw.line(self.screen, color, (x1, y1), (x2, y2))

    def text(self, x, y, text, color):
        self._require_screen("pygame_text")
        surf = self._font.render(text, True, color)
        self.screen.blit(surf, (x, y))

    def flip(self):
        self._require_screen("pygame_flip")
        self._pygame.display.flip()

    def poll_quit(self):
        self._require_screen("pygame_poll_quit")
        for event in self._pygame.event.get():
            if event.type == self._pygame.QUIT:
                self._quit_requested = True
        return self._quit_requested

    def key_down(self, key_name):
        self._require_screen("pygame_key")
        code = self._KEY_NAMES.get(key_name.lower())
        if code is None:
            self._err(f"pygame_key(): unknown key name '{key_name}'")
        pressed = self._pygame.key.get_pressed()
        return bool(pressed[code])

    def tick(self, fps):
        self._require_screen("pygame_tick")
        if fps <= 0:
            self._err("pygame_tick(): fps must be positive")
        return float(self.clock.tick(fps))

    # ---- textures (Pillow -> pygame surface) ---------------------------

    def load_image(self, path):
        self._require_screen("pygame_load_image")
        try:
            from PIL import Image
        except ImportError:
            self._err("Pillow is not installed. Install it with: pip install Pillow")
        try:
            img = Image.open(path).convert("RGBA")
        except FileNotFoundError:
            self._err(f"pygame_load_image(): no such file '{path}'")
        except Exception as e:
            self._err(f"pygame_load_image(): could not load '{path}' ({e})")
        surf = self._pygame.image.fromstring(img.tobytes(), img.size, "RGBA")
        handle = self._next_handle
        self._next_handle += 1
        self._images[handle] = surf
        return handle

    def draw_image(self, handle, x, y):
        self._require_screen("pygame_draw_image")
        surf = self._images.get(handle)
        if surf is None:
            self._err(f"pygame_draw_image(): invalid image handle {handle}")
        self.screen.blit(surf, (x, y))

    def image_size(self, handle):
        self._require_screen("pygame_image_size")
        surf = self._images.get(handle)
        if surf is None:
            self._err(f"pygame_image_size(): invalid image handle {handle}")
        w, h = surf.get_size()
        return w, h

    # ---- audio -----------------------------------------------------

    def _ensure_mixer(self, caller):
        self._require_screen(caller)
        if not self._mixer_ready:
            try:
                self._pygame.mixer.init()
                self._mixer_ready = True
            except self._pygame.error as e:
                self._err(f"{caller}(): could not initialize audio ({e})")

    def load_sound(self, path):
        self._ensure_mixer("pygame_load_sound")
        try:
            sound = self._pygame.mixer.Sound(path)
        except FileNotFoundError:
            self._err(f"pygame_load_sound(): no such file '{path}'")
        except self._pygame.error as e:
            self._err(f"pygame_load_sound(): could not load '{path}' ({e})")
        handle = self._next_handle
        self._next_handle += 1
        self._sounds[handle] = sound
        return handle

    def play_sound(self, handle):
        self._ensure_mixer("pygame_play_sound")
        sound = self._sounds.get(handle)
        if sound is None:
            self._err(f"pygame_play_sound(): invalid sound handle {handle}")
        sound.play()

    def stop_sound(self, handle):
        self._ensure_mixer("pygame_stop_sound")
        sound = self._sounds.get(handle)
        if sound is None:
            self._err(f"pygame_stop_sound(): invalid sound handle {handle}")
        sound.stop()

    def set_volume(self, handle, volume):
        self._ensure_mixer("pygame_set_volume")
        sound = self._sounds.get(handle)
        if sound is None:
            self._err(f"pygame_set_volume(): invalid sound handle {handle}")
        if volume < 0.0 or volume > 1.0:
            self._err("pygame_set_volume(): volume must be between 0.0 and 1.0")
        sound.set_volume(volume)


class CVM:
    """Executes a loaded CVM module."""

    def __init__(self, module, out=None):
        self.module = module
        self.instructions = module.instructions
        self.strings = module.strings
        self.out = out if out is not None else sys.stdout

        self.stack = []
        self.globals = {}
        self.frames = []  # call stack of Frame objects; [] means top-level scope
        self._pending_return_addr = None  # set by CALL just before jumping; consumed by MAKE_FRAME
        self.pc = 0

        self.func_table = module.functions
        self.gfx = None  # lazily-created _PygameBackend, only if a pygame_* builtin is actually called

    # ---- helpers -----------------------------------------------------

    def _err(self, msg):
        line = self.instructions[self.pc].line if self.pc < len(self.instructions) else None
        raise CRTRuntimeError(msg, line)

    def _pop(self):
        if not self.stack:
            self._err("stack underflow")
        return self.stack.pop()

    def _push(self, v):
        self.stack.append(v)

    def _current_scope(self):
        return self.frames[-1].locals if self.frames else self.globals

    def _load(self, name):
        if self.frames and name in self.frames[-1].locals:
            return self.frames[-1].locals[name]
        if name in self.globals:
            return self.globals[name]
        self._err(f"undefined variable '{name}'")

    def _store(self, name, val):
        # Plain assignment (x = ...): write through to wherever the name
        # is already bound -- local first, then global -- so mutating a
        # variable from inside a function updates the outer binding
        # instead of silently shadowing it with a new local. Only creates
        # a new binding (in the current scope) if the name isn't bound
        # anywhere yet.
        if self.frames and name in self.frames[-1].locals:
            self.frames[-1].locals[name] = val
        elif name in self.globals:
            self.globals[name] = val
        else:
            self._current_scope()[name] = val

    def _store_local(self, name, val):
        # Declaration / parameter binding (let x = ..., or a function
        # parameter): always creates/overwrites in the current scope,
        # even if a variable with the same name exists further out.
        self._current_scope()[name] = val

    @staticmethod
    def _truthy(v):
        if v is None:
            return False
        if isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            return v != 0
        if isinstance(v, str):
            return len(v) > 0
        if isinstance(v, (list, dict)):
            return len(v) > 0
        return True

    @staticmethod
    def _type_name(v):
        if v is None:
            return "null"
        if isinstance(v, bool):
            return "bool"
        if isinstance(v, int):
            return "int"
        if isinstance(v, float):
            return "float"
        if isinstance(v, str):
            return "string"
        if isinstance(v, list):
            return "list"
        if isinstance(v, dict):
            return "object"
        return "unknown"

    def _to_display(self, v):
        if v is None:
            return "null"
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, list):
            return "[" + ", ".join(self._to_display(x) for x in v) + "]"
        if isinstance(v, dict):
            return "{" + ", ".join(f"{k}: {self._to_display(x)}" for k, x in v.items()) + "}"
        if isinstance(v, float):
            if v == int(v) and abs(v) < 1e16:
                return f"{v:.1f}"
            return repr(v)
        return str(v)

    # ---- arithmetic / comparisons --------------------------------------

    def _arith(self, op):
        b = self._pop()
        a = self._pop()
        try:
            if op == "ADD":
                if isinstance(a, str) or isinstance(b, str):
                    self._push(self._to_display(a) + self._to_display(b) if not (isinstance(a, str) and isinstance(b, str)) else a + b)
                elif isinstance(a, list) and isinstance(b, list):
                    self._push(a + b)
                else:
                    self._push(a + b)
            elif op == "SUB":
                self._push(a - b)
            elif op == "MUL":
                self._push(a * b)
            elif op == "DIV":
                if b == 0:
                    self._err("division by zero")
                if isinstance(a, int) and isinstance(b, int) and a % b == 0:
                    self._push(a // b)
                else:
                    self._push(a / b)
            elif op == "MOD":
                if b == 0:
                    self._err("modulo by zero")
                self._push(a % b)
        except TypeError:
            self._err(f"unsupported operand types for {op}: {self._type_name(a)} and {self._type_name(b)}")

    def _compare(self, op):
        b = self._pop()
        a = self._pop()
        if op == "EQ":
            self._push(a == b)
        elif op == "NEQ":
            self._push(a != b)
        else:
            try:
                if op == "LT":
                    self._push(a < b)
                elif op == "GT":
                    self._push(a > b)
                elif op == "LTE":
                    self._push(a <= b)
                elif op == "GTE":
                    self._push(a >= b)
            except TypeError:
                self._err(f"cannot compare {self._type_name(a)} and {self._type_name(b)}")

    # ---- builtins -----------------------------------------------------

    def _call_builtin(self, name, argc):
        args = [self._pop() for _ in range(argc)][::-1]

        def need(n):
            if len(args) != n:
                self._err(f"{name}() takes {n} argument(s), got {len(args)}")

        if name == "len":
            need(1)
            v = args[0]
            if isinstance(v, (str, list, dict)):
                self._push(len(v))
            else:
                self._err(f"len() not supported for type {self._type_name(v)}")
        elif name == "str":
            need(1)
            self._push(self._to_display(args[0]))
        elif name == "int":
            need(1)
            v = args[0]
            try:
                if isinstance(v, str):
                    self._push(int(v.strip()))
                elif isinstance(v, bool):
                    self._push(1 if v else 0)
                else:
                    self._push(int(v))
            except (ValueError, TypeError):
                self._err(f"cannot convert {self._type_name(v)} to int")
        elif name == "float":
            need(1)
            v = args[0]
            try:
                self._push(float(v))
            except (ValueError, TypeError):
                self._err(f"cannot convert {self._type_name(v)} to float")
        elif name == "type":
            need(1)
            self._push(self._type_name(args[0]))
        elif name == "upper":
            need(1)
            if not isinstance(args[0], str):
                self._err("upper() requires a string")
            self._push(args[0].upper())
        elif name == "lower":
            need(1)
            if not isinstance(args[0], str):
                self._err("lower() requires a string")
            self._push(args[0].lower())
        elif name == "json_encode":
            need(1)
            try:
                self._push(json.dumps(args[0]))
            except TypeError as e:
                self._err(f"json_encode(): cannot encode value ({e})")
        elif name == "json_decode":
            need(1)
            if not isinstance(args[0], str):
                self._err("json_decode() requires a string")
            try:
                self._push(json.loads(args[0]))
            except json.JSONDecodeError as e:
                self._err(f"json_decode(): invalid JSON ({e})")
        elif name == "file_read":
            need(1)
            if not isinstance(args[0], str):
                self._err("file_read() requires a string path")
            try:
                with open(args[0], "r", encoding="utf-8") as f:
                    self._push(f.read())
            except FileNotFoundError:
                self._err(f"file_read(): no such file '{args[0]}'")
            except OSError as e:
                self._err(f"file_read(): {e}")
        elif name == "file_write":
            need(2)
            path, content = args
            if not isinstance(path, str):
                self._err("file_write() requires a string path")
            if not isinstance(content, str):
                content = self._to_display(content)
            try:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(content)
                self._push(None)
            except OSError as e:
                self._err(f"file_write(): {e}")
        elif name == "file_append":
            need(2)
            path, content = args
            if not isinstance(path, str):
                self._err("file_append() requires a string path")
            if not isinstance(content, str):
                content = self._to_display(content)
            try:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(content)
                self._push(None)
            except OSError as e:
                self._err(f"file_append(): {e}")
        elif name == "file_exists":
            need(1)
            if not isinstance(args[0], str):
                self._err("file_exists() requires a string path")
            self._push(os.path.isfile(args[0]))
        elif name == "file_delete":
            need(1)
            if not isinstance(args[0], str):
                self._err("file_delete() requires a string path")
            try:
                os.remove(args[0])
                self._push(None)
            except FileNotFoundError:
                self._err(f"file_delete(): no such file '{args[0]}'")
            except OSError as e:
                self._err(f"file_delete(): {e}")
        else:
            self._err(f"unknown builtin '{name}'")

    def _call_gfx(self, name, argc):
        args = [self._pop() for _ in range(argc)][::-1]

        def as_int(v, what):
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                self._err(f"{name}(): expected a number for {what}, got {self._type_name(v)}")
            return int(v)

        def as_color(r, g, b):
            return (as_int(r, "r") & 255, as_int(g, "g") & 255, as_int(b, "b") & 255)

        if self.gfx is None:
            self.gfx = _PygameBackend(self._err)

        if name == "pygame_init":
            w, h, title = args
            self.gfx.init(as_int(w, "width"), as_int(h, "height"), self._to_display(title) if not isinstance(title, str) else title)
            self._push(None)
        elif name == "pygame_quit":
            self.gfx.quit()
            self._push(None)
        elif name == "pygame_clear":
            if len(args) == 0:
                self.gfx.clear((0, 0, 0))
            else:
                r, g, b = args
                self.gfx.clear(as_color(r, g, b))
            self._push(None)
        elif name == "pygame_rect":
            x, y, w, h, r, g, b = args
            self.gfx.rect(as_int(x, "x"), as_int(y, "y"), as_int(w, "w"), as_int(h, "h"), as_color(r, g, b))
            self._push(None)
        elif name == "pygame_circle":
            x, y, radius, r, g, b = args
            self.gfx.circle(as_int(x, "x"), as_int(y, "y"), as_int(radius, "radius"), as_color(r, g, b))
            self._push(None)
        elif name == "pygame_line":
            x1, y1, x2, y2, r, g, b = args
            self.gfx.line(as_int(x1, "x1"), as_int(y1, "y1"), as_int(x2, "x2"), as_int(y2, "y2"), as_color(r, g, b))
            self._push(None)
        elif name == "pygame_text":
            x, y, text, r, g, b = args
            if not isinstance(text, str):
                text = self._to_display(text)
            self.gfx.text(as_int(x, "x"), as_int(y, "y"), text, as_color(r, g, b))
            self._push(None)
        elif name == "pygame_flip":
            self.gfx.flip()
            self._push(None)
        elif name == "pygame_poll_quit":
            self._push(self.gfx.poll_quit())
        elif name == "pygame_key":
            key_name = args[0]
            if not isinstance(key_name, str):
                self._err("pygame_key(): expected a string key name")
            self._push(self.gfx.key_down(key_name))
        elif name == "pygame_tick":
            fps = args[0]
            if isinstance(fps, bool) or not isinstance(fps, (int, float)):
                self._err("pygame_tick(): expected a number for fps")
            self._push(self.gfx.tick(fps))
        elif name == "pygame_load_image":
            path = args[0]
            if not isinstance(path, str):
                self._err("pygame_load_image() requires a string path")
            self._push(self.gfx.load_image(path))
        elif name == "pygame_draw_image":
            handle, x, y = args
            self.gfx.draw_image(as_int(handle, "handle"), as_int(x, "x"), as_int(y, "y"))
            self._push(None)
        elif name == "pygame_image_size":
            handle = args[0]
            w, h = self.gfx.image_size(as_int(handle, "handle"))
            self._push({"w": w, "h": h})
        elif name == "pygame_load_sound":
            path = args[0]
            if not isinstance(path, str):
                self._err("pygame_load_sound() requires a string path")
            self._push(self.gfx.load_sound(path))
        elif name == "pygame_play_sound":
            handle = args[0]
            self.gfx.play_sound(as_int(handle, "handle"))
            self._push(None)
        elif name == "pygame_stop_sound":
            handle = args[0]
            self.gfx.stop_sound(as_int(handle, "handle"))
            self._push(None)
        elif name == "pygame_set_volume":
            handle, volume = args
            if isinstance(volume, bool) or not isinstance(volume, (int, float)):
                self._err("pygame_set_volume(): expected a number for volume")
            self.gfx.set_volume(as_int(handle, "handle"), float(volume))
            self._push(None)
        else:
            self._err(f"unknown graphics builtin '{name}'")

    # ---- main loop -----------------------------------------------------

    def run(self):
        instrs = self.instructions
        n = len(instrs)
        while self.pc < n:
            ins = instrs[self.pc]
            op = REV_OPCODES.get(ins.op)
            if op is None:
                self._err(f"invalid opcode 0x{ins.op:02x}")

            if op == "HALT":
                return
            elif op == "PUSH_INT":
                self._push(ins.arg_val)
            elif op == "PUSH_FLOAT":
                self._push(float(self.strings[ins.arg_val]))
            elif op == "PUSH_STR":
                self._push(self.strings[ins.arg_val])
            elif op == "PUSH_BOOL":
                self._push(bool(ins.arg_val))
            elif op == "PUSH_NULL":
                self._push(None)
            elif op == "POP":
                self._pop()
            elif op == "DUP":
                if not self.stack:
                    self._err("stack underflow")
                self._push(self.stack[-1])
            elif op == "LOAD":
                self._push(self._load(self.strings[ins.arg_val]))
            elif op == "STORE":
                self._store(self.strings[ins.arg_val], self._pop())
            elif op == "STORE_LOCAL":
                self._store_local(self.strings[ins.arg_val], self._pop())
            elif op in ("ADD", "SUB", "MUL", "DIV", "MOD"):
                self._arith(op)
            elif op in ("EQ", "NEQ", "LT", "GT", "LTE", "GTE"):
                self._compare(op)
            elif op == "NEG":
                v = self._pop()
                if not isinstance(v, (int, float)) or isinstance(v, bool):
                    self._err(f"cannot negate {self._type_name(v)}")
                self._push(-v)
            elif op == "NOT":
                self._push(not self._truthy(self._pop()))
            elif op == "PRINT":
                self.out.write(self._to_display(self._pop()) + "\n")
            elif op == "JUMP":
                self.pc = ins.arg_val
                continue
            elif op == "JUMP_IF_FALSE":
                if not self._truthy(self._pop()):
                    self.pc = ins.arg_val
                    continue
            elif op == "JUMP_IF_TRUE":
                if self._truthy(self._pop()):
                    self.pc = ins.arg_val
                    continue
            elif op == "MAKE_LIST":
                count = ins.arg_val
                items = [self._pop() for _ in range(count)][::-1]
                self._push(items)
            elif op == "MAKE_OBJECT":
                count = ins.arg_val
                obj = {}
                pairs = []
                for _ in range(count):
                    val = self._pop()
                    key = self._pop()
                    pairs.append((key, val))
                for key, val in reversed(pairs):
                    obj[key] = val
                self._push(obj)
            elif op == "GET_MEMBER":
                obj = self._pop()
                name = self.strings[ins.arg_val]
                if not isinstance(obj, dict):
                    self._err(f"cannot access member '{name}' on {self._type_name(obj)}")
                if name not in obj:
                    self._err(f"object has no member '{name}'")
                self._push(obj[name])
            elif op == "SET_MEMBER":
                val = self._pop()
                obj = self._pop()
                name = self.strings[ins.arg_val]
                if not isinstance(obj, dict):
                    self._err(f"cannot set member '{name}' on {self._type_name(obj)}")
                obj[name] = val
            elif op == "GET_INDEX":
                idx = self._pop()
                obj = self._pop()
                try:
                    if isinstance(obj, list):
                        if not isinstance(idx, int) or isinstance(idx, bool):
                            self._err("list index must be an int")
                        self._push(obj[idx])
                    elif isinstance(obj, dict):
                        self._push(obj[idx])
                    elif isinstance(obj, str):
                        self._push(obj[idx])
                    else:
                        self._err(f"cannot index into {self._type_name(obj)}")
                except IndexError:
                    self._err("index out of range")
                except KeyError:
                    self._err(f"object has no key '{idx}'")
            elif op == "SET_INDEX":
                val = self._pop()
                idx = self._pop()
                obj = self._pop()
                if isinstance(obj, list):
                    if not isinstance(idx, int) or isinstance(idx, bool):
                        self._err("list index must be an int")
                    # Match GET_INDEX's Python-style negative indexing
                    # (l[-1] reads the last element) instead of only
                    # allowing it for reads and erroring on writes.
                    norm_idx = idx + len(obj) if idx < 0 else idx
                    if norm_idx < 0 or norm_idx >= len(obj):
                        self._err("index out of range")
                    obj[norm_idx] = val
                elif isinstance(obj, dict):
                    obj[idx] = val
                else:
                    self._err(f"cannot assign into {self._type_name(obj)}")
            elif op == "MAKE_FRAME":
                # Enter a new local scope for a function call. The CALL
                # opcode already pushed the arguments and jumped here; this
                # instruction opens the local frame so the STOREs that
                # immediately follow (one per parameter, compiled in
                # reverse) bind into local scope instead of globals.
                self.frames.append(Frame(return_addr=self._pending_return_addr))
            elif op == "CALL":
                name = self.strings[ins.arg_val]
                argc = ins.extra
                if name not in self.func_table:
                    self._err(f"call to undefined function '{name}'")
                addr, arity = self.func_table[name]
                if argc != arity:
                    self._err(f"'{name}' takes {arity} argument(s), got {argc}")
                args = [self._pop() for _ in range(argc)][::-1]
                self._pending_return_addr = self.pc + 1
                self.pc = addr
                # args is now in original left-to-right parameter order,
                # e.g. [a_val, b_val]. The function body's STOREs run in
                # reversed(params) order (last param first), so the LAST
                # param's value must be on top of the stack. Pushing args
                # in order (not reversed) puts a_val down first and b_val
                # on top -- exactly what the first STORE (for b) needs.
                for a in args:
                    self._push(a)
                continue
            elif op == "CALL_BUILTIN":
                name = self.strings[ins.arg_val]
                self._call_builtin(name, ins.extra)
            elif op == "CALL_GFX":
                name = self.strings[ins.arg_val]
                self._call_gfx(name, ins.extra)
            elif op == "RET":
                retval = self._pop()
                if not self.frames:
                    self._err("'return' outside of a function")
                frame = self.frames.pop()
                self.pc = frame.return_addr
                self._push(retval)
                continue
            else:
                self._err(f"unimplemented opcode: {op}")

            self.pc += 1


def run_module(module, out=None):
    CVM(module, out=out).run()


def run_source(code, out=None):
    """Convenience: compile then immediately execute source text."""
    data = compile_source(code)
    module = load_module(data)
    run_module(module, out=out)


# =====================================================================
# 5. CLI / REPL / DISASSEMBLER
# =====================================================================

def disassemble(module, file=sys.stdout):
    """Human-readable listing of a loaded CVM module."""
    file.write(f"; CVM disassembly ({len(module.instructions)} instructions, "
               f"{len(module.strings)} strings)\n")
    if module.strings:
        file.write(";\n; string table:\n")
        for i, s in enumerate(module.strings):
            file.write(f";   [{i}] {s!r}\n")
    file.write(";\n")
    for addr, ins in enumerate(module.instructions):
        name = REV_OPCODES.get(ins.op, f"0x{ins.op:02x}")
        operand = ""
        if ins.arg_type == ARG_STR:
            s = module.strings[ins.arg_val] if 0 <= ins.arg_val < len(module.strings) else "?"
            operand = f" {ins.arg_val} ; {s!r}"
        elif ins.arg_type == ARG_IMM:
            operand = f" {ins.arg_val}"
        extra = f"  (extra={ins.extra})" if ins.extra else ""
        file.write(f"{addr:6d}: {name:<16}{operand}{extra}\n")


def _read_file(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except OSError as e:
        print(f"crt: cannot read '{path}': {e}", file=sys.stderr)
        sys.exit(1)


def cmd_run(args):
    code = _read_file(args.file)
    try:
        run_source(code)
    except CRTError as e:
        print(f"crt: {e.format()}", file=sys.stderr)
        sys.exit(1)


def cmd_build(args):
    code = _read_file(args.file)
    try:
        data = compile_source(code)
    except CRTError as e:
        print(f"crt: {e.format()}", file=sys.stderr)
        sys.exit(1)
    out_path = args.output or (os.path.splitext(args.file)[0] + ".cvm")
    with open(out_path, "wb") as f:
        f.write(data)
    print(f"crt: wrote {out_path} ({len(data)} bytes)")


def cmd_exec(args):
    try:
        with open(args.file, "rb") as f:
            data = f.read()
    except OSError as e:
        print(f"crt: cannot read '{args.file}': {e}", file=sys.stderr)
        sys.exit(1)
    try:
        module = load_module(data)
        run_module(module)
    except CRTError as e:
        print(f"crt: {e.format()}", file=sys.stderr)
        sys.exit(1)


def cmd_disasm(args):
    try:
        with open(args.file, "rb") as f:
            data = f.read()
    except OSError as e:
        print(f"crt: cannot read '{args.file}': {e}", file=sys.stderr)
        sys.exit(1)
    try:
        module = load_module(data)
    except CRTError as e:
        print(f"crt: {e.format()}", file=sys.stderr)
        sys.exit(1)
    disassemble(module)


def cmd_asm(args):
    text = _read_file(args.file)
    try:
        data = assemble_source(text)
    except CRTError as e:
        print(f"crt: {e.format()}", file=sys.stderr)
        sys.exit(1)
    out_path = args.output or (os.path.splitext(args.file)[0] + ".cvm")
    with open(out_path, "wb") as f:
        f.write(data)
    print(f"crt: wrote {out_path} ({len(data)} bytes)")


CHEAT_SHEET = """\
CRT CHEAT SHEET
================

VARIABLES
    let x = 5;                 declare
    x = 6;                     reassign (walks up to an existing outer
                                binding first; only creates a new local
                                if the name isn't bound anywhere yet)

TYPES
    5            int
    3.5          float
    "hi"  'hi'   string          (both quote styles work)
    true  false  bool
    null         null
    [1, 2, 3]    list
    {a: 1}       object

OPERATORS
    + - * / %              arithmetic (/ gives a float unless exact)
    == != < > <= >=        comparison
    && || !                logic       (also: and, or, not)
    -x                     unary minus

STRINGS
    "a" + "b"               concatenation
    "line\\nbreak\\ttab"     escapes: \\n \\t \\r \\" \\' \\\\ \\0

COMMENTS
    // line comment
    /* block
       comment */

CONTROL FLOW
    if (cond) { ... } else if (cond) { ... } else { ... }
    while (cond) { ... }
    for (let i = 0; i < 10; i = i + 1) { ... }
    break;
    continue;

FUNCTIONS
    fn add(a, b) {
        return a + b;
    }
    add(1, 2);                  recursion and mutual recursion both work

LISTS & OBJECTS
    let l = [1, 2, 3];
    l[0];  l[0] = 9;             negative indices work too: l[-1]
    let o = {name: "crt", n: 1};
    o.name;  o.name = "x";
    o["name"];  o["name"] = "x"; bracket access works for objects too

BUILT-IN FUNCTIONS
    len(x)          length of a string, list, or object
    str(x)           convert to string
    int(x)            convert to int
    float(x)           convert to float
    type(x)             "int" "float" "string" "bool" "null" "list" "object"
    upper(s)  lower(s)   string case

    json_encode(x)        encode a value as a JSON string
    json_decode(s)          parse a JSON string into a value

    file_read(path)              -> file contents as a string
    file_write(path, content)     overwrite a file with content
    file_append(path, content)     append content to a file
    file_exists(path)                -> true/false
    file_delete(path)                  delete a file

GRAPHICS (pygame mode) -- see `crt demo graphics` for full detail,
including textures and audio
    pygame_init(w, h, title)     open a window
    pygame_clear()                 clear to black
    pygame_clear(r, g, b)           clear to a color
    pygame_rect(x, y, w, h, r, g, b)
    pygame_circle(x, y, radius, r, g, b)
    pygame_line(x1, y1, x2, y2, r, g, b)
    pygame_text(x, y, text, r, g, b)
    pygame_flip()                  show the frame you just drew
    pygame_poll_quit()               true if the window's X was clicked
    pygame_key(name)                   true if that key is held
    pygame_tick(fps)                     caps framerate, returns ms elapsed
    pygame_quit()                          close the window
    pygame_load_image / draw_image / image_size    textures (needs Pillow)
    pygame_load_sound / play_sound / stop_sound / set_volume    audio

CLI
    crt run file.ct                  run a .ct file directly
    crt build file.ct -o file.cvm    compile to bytecode
    crt exec file.cvm                run compiled bytecode
    crt disasm file.cvm              disassemble bytecode
    crt asm file.asm -o file.cvm     assemble CRT assembly to bytecode
    crt                              interactive REPL
    crt demo                         this cheat sheet
    crt demo graphics                graphics + textures + audio cheat sheet
    crt demo asm                     CRT assembly language cheat sheet
"""

GRAPHICS_CHEAT_SHEET = """\
CRT GRAPHICS CHEAT SHEET (pygame mode)
=======================================

Calling any pygame_* function opens a real window and enters graphics
mode. These are ordinary builtin functions -- usable anywhere a normal
call is, with the same argument-count checking as user functions.

    pygame_init(width, height, title)
        Opens the window. Call this first, once.

    pygame_clear()
        Clears the frame to black. Shorthand for pygame_clear(0, 0, 0).

    pygame_clear(r, g, b)
        Clears/fills the whole frame with a color. Call this at the
        start of every loop iteration before drawing, the same way you'd
        clear a canvas before repainting it.

    pygame_rect(x, y, w, h, r, g, b)
        Filled rectangle, top-left corner at (x, y).

    pygame_circle(x, y, radius, r, g, b)
        Filled circle centered at (x, y).

    pygame_line(x1, y1, x2, y2, r, g, b)
        Line from (x1, y1) to (x2, y2).

    pygame_text(x, y, text, r, g, b)
        Draws text with its top-left at (x, y).

    pygame_flip()
        Presents everything you drew this frame. Nothing appears on
        screen until you call this.

    pygame_poll_quit()
        Returns true once, the moment the window's close button (X) is
        clicked. Check this every loop iteration to exit cleanly.

    pygame_key(name)
        Returns true while a key is held down. Names: left right up
        down space enter escape shift ctrl tab, or any single letter
        or digit ("a".."z", "0".."9").

    pygame_tick(fps)
        Caps the loop to roughly `fps` frames per second and returns
        the milliseconds elapsed since the last call. Call it once per
        loop iteration, usually last.

    pygame_quit()
        Closes the window. Call this after the loop ends.

Colors are (r, g, b), each 0-255. Coordinates accept floats and are
truncated to whole pixels automatically.

TEXTURES (needs Pillow: pip install Pillow)
    pygame_load_image(path)
        Loads an image file into a texture and returns an integer
        handle to it. Call once, ahead of time (e.g. before the loop).

    pygame_draw_image(handle, x, y)
        Draws a loaded texture with its top-left corner at (x, y).

    pygame_image_size(handle)
        Returns {w: ..., h: ...} for a loaded texture.

AUDIO (uses pygame's mixer, initialized lazily on first use)
    pygame_load_sound(path)
        Loads a sound file (wav/ogg) and returns an integer handle.

    pygame_play_sound(handle)
        Plays a loaded sound. Can be called repeatedly to layer plays.

    pygame_stop_sound(handle)
        Stops a loaded sound if it's currently playing.

    pygame_set_volume(handle, volume)
        Sets a loaded sound's volume, 0.0 (silent) to 1.0 (full).

MINIMAL GAME LOOP
    pygame_init(640, 480, "My Game");
    let running = true;
    while (running) {
        if (pygame_poll_quit()) { running = false; }
        if (pygame_key("escape")) { running = false; }

        // ...update state here...

        pygame_clear();
        pygame_circle(100, 100, 20, 255, 0, 0);
        pygame_flip();
        pygame_tick(60);
    }
    pygame_quit();
"""

ASM_CHEAT_SHEET = """\
CRT ASSEMBLY CHEAT SHEET
=========================

`crt asm file.asm -o file.cvm` assembles hand-written CVM assembly text
into the same .cvm bytecode format `crt build` produces -- runnable with
`crt exec` and inspectable with `crt disasm`. A `crt disasm` listing is
close enough to valid assembly that editing one is a good way to learn.

SYNTAX
    ; a comment, to end of line
    label:                  define a label (address of the next
                             instruction) -- usable as a jump target
    .func name arity        mark the next instruction's address as the
                             entry point of function `name`, which takes
                             `arity` parameters
    MNEMONIC                instruction with no operand
    MNEMONIC 42              instruction with an integer operand
    MNEMONIC "text"           instruction with a string operand
    MNEMONIC 3.5               float literal operand
    MNEMONIC label               a label used as a jump target
    MNEMONIC "name", 2             string operand plus an extra field
                                     (e.g. argument count for CALL)

Any bare identifier operand that isn't a defined label is an error
(undefined label) rather than silently assembling as address 0.

INSTRUCTIONS
    PUSH_INT n            PUSH_FLOAT f          PUSH_STR "s"
    PUSH_BOOL 0/1         PUSH_NULL             DUP  POP
    ADD SUB MUL DIV MOD                         (pop 2, push 1)
    EQ NEQ LT GT LTE GTE                        (pop 2, push 1)
    NEG NOT                                     (pop 1, push 1)
    LOAD "name"           STORE "name"          STORE_LOCAL "name"
    JUMP addr             JUMP_IF_TRUE addr     JUMP_IF_FALSE addr
    MAKE_FRAME n          RET                   HALT
    CALL "name", argc     CALL_BUILTIN "name", argc
    CALL_GFX "name", argc (pygame_* builtins)
    MAKE_LIST n           MAKE_OBJECT n
    GET_INDEX  SET_INDEX  GET_MEMBER "k"  SET_MEMBER "k"

MINIMAL EXAMPLE (prints 15)
    PUSH_INT 5
    PUSH_INT 10
    ADD
    PRINT
    HALT

A FUNCTION, CALLED FROM TOP LEVEL
    JUMP main            ; skip over the function body
.func add 2
add:
    MAKE_FRAME 2
    STORE_LOCAL "b"      ; params pop off in reverse order
    STORE_LOCAL "a"
    LOAD "a"
    LOAD "b"
    ADD
    RET
main:
    PUSH_INT 3
    PUSH_INT 4
    CALL "add", 2
    PRINT
    HALT
"""


def cmd_demo(args):
    topic = getattr(args, "topic", None)
    if topic == "graphics":
        print(GRAPHICS_CHEAT_SHEET, end="")
    elif topic == "asm":
        print(ASM_CHEAT_SHEET, end="")
    else:
        print(CHEAT_SHEET, end="")


def repl():
    print("crt REPL - type 'exit' or Ctrl-D to quit")
    vm_globals = {}
    fn_source = []   # accumulated function declarations (persist across turns)
    pending = ""
    while True:
        try:
            prompt = "crt> " if not pending else "...> "
            line = input(prompt)
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not pending and line.strip() in ("exit", "quit"):
            break
        pending += line + "\n"
        if pending.count("{") > pending.count("}"):
            continue
        source = pending
        pending = ""
        try:
            tokens = tokenize(source)
            ast = Parser(tokens).parse()
        except CRTError as e:
            print(f"error: {e.format()}")
            continue

        new_fn_decls = [s for s in ast.stmts if isinstance(s, FuncDeclNode)]
        other_stmts = [s for s in ast.stmts if not isinstance(s, FuncDeclNode)]

        # Recompile every function ever declared this session plus just the
        # new non-function statements from this turn, so earlier fn defs
        # stay callable without re-running earlier print()s etc.
        full_ast = ProgramNode(fn_source + new_fn_decls + other_stmts)
        try:
            compiler = BinaryCompiler()
            compiler.compile_program(full_ast)
            _verify_calls(full_ast, compiler)
            data = compiler.assemble()
            module = load_module(data)
            vm = CVM(module)
            vm.globals = vm_globals
            vm.run()
            vm_globals.update(vm.globals)
            fn_source.extend(new_fn_decls)
        except CRTError as e:
            print(f"error: {e.format()}")
        except Exception as e:  # pragma: no cover - safety net for REPL use
            print(f"internal error: {e}")


def main(argv=None):
    parser = argparse.ArgumentParser(prog="crt", description="CRT language toolchain")
    sub = parser.add_subparsers(dest="command")

    p_run = sub.add_parser("run", help="Run a .ct source file directly")
    p_run.add_argument("file")
    p_run.set_defaults(func=cmd_run)

    p_build = sub.add_parser("build", help="Compile a .ct file to .cvm bytecode")
    p_build.add_argument("file")
    p_build.add_argument("-o", "--output", help="Output .cvm path")
    p_build.set_defaults(func=cmd_build)

    p_exec = sub.add_parser("exec", help="Execute a compiled .cvm file")
    p_exec.add_argument("file")
    p_exec.set_defaults(func=cmd_exec)

    p_disasm = sub.add_parser("disasm", help="Disassemble a compiled .cvm file")
    p_disasm.add_argument("file")
    p_disasm.set_defaults(func=cmd_disasm)

    p_asm = sub.add_parser("asm", help="Assemble a .asm file to .cvm bytecode")
    p_asm.add_argument("file")
    p_asm.add_argument("-o", "--output", help="Output .cvm path")
    p_asm.set_defaults(func=cmd_asm)

    p_demo = sub.add_parser("demo", help="Print a CRT language cheat sheet")
    p_demo.add_argument("topic", nargs="?", choices=["graphics", "asm"],
                         help="Optional: 'graphics' or 'asm' for those cheat sheets")
    p_demo.set_defaults(func=cmd_demo)

    args = parser.parse_args(argv)
    if not getattr(args, "command", None):
        repl()
        return
    args.func(args)


if __name__ == "__main__":
    main()