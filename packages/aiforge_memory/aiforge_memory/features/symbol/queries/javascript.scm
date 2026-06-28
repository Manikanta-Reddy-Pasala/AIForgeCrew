; tree-sitter JavaScript query — symbols + imports + calls
;
; JS has no `interface_declaration` or `type_identifier` (those are
; TypeScript-only). Using the TS query against JS yielded 0 symbol
; matches because those node types never appear in plain JS trees.

; class
(class_declaration
  name: (identifier) @class.name) @class.def

; method on a class
(method_definition
  name: (property_identifier) @method.name) @method.def

; top-level function
(function_declaration
  name: (identifier) @function.name) @function.def

; arrow / variable bound function
(variable_declarator
  name: (identifier) @function.name
  value: [(arrow_function) (function_expression)]) @function.def

; ES module import — `import X from "module"` / `import { X } from "module"`
(import_statement
  source: (string (string_fragment) @import.module))

; CommonJS require — `const X = require("module")`
(call_expression
  function: (identifier) @_fn
  arguments: (arguments
    (string (string_fragment) @import.module))
  (#eq? @_fn "require"))

; call expression
(call_expression
  function: [
    (identifier)            @call.name
    (member_expression
      property: (property_identifier) @call.name)
  ]) @call
