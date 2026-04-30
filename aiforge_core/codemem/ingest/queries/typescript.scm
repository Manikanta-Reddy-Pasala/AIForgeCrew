; tree-sitter TypeScript query — symbols + imports + calls

; class
(class_declaration
  name: (type_identifier) @class.name) @class.def

; interface
(interface_declaration
  name: (type_identifier) @class.name) @class.def

; method
(method_definition
  name: (property_identifier) @method.name) @method.def

; top-level function
(function_declaration
  name: (identifier) @function.name) @function.def

; arrow / variable bound function
(variable_declarator
  name: (identifier) @function.name
  value: [(arrow_function) (function_expression)]) @function.def

; import { X } from "module"
(import_statement
  source: (string (string_fragment) @import.module))

; call expression
(call_expression
  function: [
    (identifier)            @call.name
    (member_expression
      property: (property_identifier) @call.name)
  ]) @call
