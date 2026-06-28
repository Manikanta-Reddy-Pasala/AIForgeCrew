; tree-sitter Python query — symbols + imports + calls
;
; Tagged captures (consumed by treesitter_walk + edges):
;   @class.def       class definition node
;   @class.name      class name identifier
;   @method.def      method definition (function inside class)
;   @method.name     method name identifier
;   @function.def    top-level function definition
;   @function.name   function name identifier
;   @import.from     "from X import Y" -> X
;   @import.module   "import X" -> X
;   @call            call expression (resolved separately)
;   @call.name       last identifier in the callee (best-effort)

; class def
(class_definition
  name: (identifier) @class.name) @class.def

; method def (function defined inside a class body)
(class_definition
  body: (block
          (function_definition
            name: (identifier) @method.name) @method.def))

; top-level function def
(module
  (function_definition
    name: (identifier) @function.name) @function.def)

; from X import Y  (absolute)
(import_from_statement
  module_name: (dotted_name) @import.from)

; from .X import Y  (relative — capture the inner dotted_name)
(import_from_statement
  module_name: (relative_import (dotted_name) @import.from))

; import X
(import_statement
  name: (dotted_name) @import.module)

; call expressions — resolved to a name later
(call
  function: [
    (identifier)        @call.name
    (attribute attribute: (identifier) @call.name)
  ]) @call
