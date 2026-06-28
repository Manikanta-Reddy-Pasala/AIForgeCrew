; tree-sitter Java query — symbols + imports + calls

; class
(class_declaration
  name: (identifier) @class.name) @class.def

; interface (captured separately so the graph distinguishes interface
; from concrete class — useful for Spring DI / mock-resolution lookups)
(interface_declaration
  name: (identifier) @interface.name) @interface.def

; enum
(enum_declaration
  name: (identifier) @enum.name) @enum.def

; annotation type
(annotation_type_declaration
  name: (identifier) @annotation.name) @annotation.def

; record (Java 14+) — treat like a class
(record_declaration
  name: (identifier) @class.name) @class.def

; method
(method_declaration
  name: (identifier) @method.name) @method.def

; constructor
(constructor_declaration
  name: (identifier) @method.name) @method.def

; field — capture each variable declarator separately so multi-var
; declarations like `private int a, b;` produce two field rows.
(field_declaration
  declarator: (variable_declarator
    name: (identifier) @field.name)) @field.def

; import statements — capture full dotted name
(import_declaration
  (scoped_identifier) @import.module)

; method invocations — `obj.method(...)` or `method(...)`
(method_invocation
  name: (identifier) @call.name) @call
