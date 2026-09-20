(lang dune 3.14)

(name protocol-emulator-compiler)

(generate_opam_files)

(source
 (github WilliamZhang20/protocol-emulator-compiler))

(package
 (name protocol-emulator-compiler)
 (synopsis "Assembler and compiler frontend for protocol-emulator")
 (description
  "OCaml assembler and compiler frontend targeting the SRAM bytecode ISA of https://github.com/WilliamZhang20/protocol-emulator.")
 (depends ocaml dune))
