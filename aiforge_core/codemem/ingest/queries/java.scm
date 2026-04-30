; tree-sitter Java query — symbols + imports + calls

; class
(class_declaration
  name: (identifier) @class.name) @class.def

; interface
(interface_declaration
  name: (identifier) @class.name) @class.def

; method (Java method_declaration appears anywhere in a class body)
(method_declaration
  name: (identifier) @method.name) @method.def

; constructor
(constructor_declaration
  name: (identifier) @method.name) @method.def

; import statements — capture full dotted name
(import_declaration
  (scoped_identifier) @import.module)

; method invocations — `obj.method(...)` or `method(...)`
(method_invocation
  name: (identifier) @call.name) @call
