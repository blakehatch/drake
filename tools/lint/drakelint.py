import os
import re
import sys

from drake.tools.lint.formatter import IncludeFormatter


def _check_unguarded_openmp_uses(filename):
    """Returns 0 if all OpenMP uses in `filename` are properly guarded by
    #if defined(_OPENMP), and 1 otherwise.
    """
    openmp_include = "#include <omp.h>"
    openmp_pragma = "#pragma omp"

    openmp_pre_guard = "#if defined(_OPENMP)"
    openmp_post_guard = "#endif"

    with open(filename, mode='r', encoding='utf-8') as file:
        lines = file.readlines()

    for index, current_line in enumerate(lines):
        if openmp_include in current_line or openmp_pragma in current_line:
            previous_line = lines[index - 1] if (index - 1) >= 0 else ""
            next_line = lines[index + 1] if (index + 1) < len(lines) else ""

            missing_pre_guard = previous_line.strip() != openmp_pre_guard
            missing_post_guard = next_line.strip() != openmp_post_guard

            if missing_pre_guard or missing_post_guard:
                print(f"ERROR: {filename}:{index + 1}: "
                      "OpenMP includes and directives must be guarded by "
                      f"{openmp_pre_guard} on the previous line and "
                      f"{openmp_post_guard} on the following line")

                return 1
    return 0


def _check_header_using_overloaded(filename):
    """Returns 1 if the file is a header and (wrongly) includes overloaded.h.
    Returns 0 otherwise."""
    forbidden_re = re.compile(
        # This expression approximates section 6.10.2 except 6.10.2.4 of
        # https://www.open-std.org/jtc1/sc22/wg14/www/docs/n1570.pdf
        r'\s*#\s*include\s*[<"]drake/common/overloaded.h\s*[>"]')
    if filename.endswith(".h"):
        with open(filename, mode='r', encoding='utf-8') as file:
            for line in file.readlines():
                if forbidden_re.match(line):
                    print("ERROR:  Header files must not include "
                          "drake/common/overloaded.h")
                    return 1
    return 0


def _check_header_doxygen_file_spelling(filename):
    """Returns 1 if the file is a header and mis-uses Doxygen's `@file` markup.
    (The `@file` markup should always be followed immediately by a newline.)
    Returns 0 otherwise."""
    message = (
        "Always add a line break after a Doxygen @file marker. "
        "The description must start on the following line."
    )
    forbidden_re = re.compile(r'\s*(///?|/\*\*?)\s+@file.')
    if filename.endswith(".h"):
        with open(filename, mode='r', encoding='utf-8') as file:
            lines = file.readlines()
        for index, current_line in enumerate(lines):
            if forbidden_re.match(current_line):
                print(f"ERROR: {filename}:{index + 1}: {message}")
                return 1
    return 0


def _check_invalid_line_endings(filename):
    """Returns 0 if all of the newlines in `filename` are Unix, and 1
    otherwise.
    """
    # Ask Python to read the file and determine the newlines convention.
    with open(filename, mode='r', encoding='utf-8') as file:
        file.read()
        if file.newlines is None:
            newlines = tuple()
        else:
            newlines = tuple(file.newlines)

    # Only allow Unix newlines.
    for newline in newlines:
        if newline != '\n':
            print("ERROR: non-Unix newline characters found")
            return 1

    return 0


def _check_includes(filename):
    """Returns 0 if clang-format-includes is a no-op, and 1 otherwise."""
    try:
        tool = IncludeFormatter(filename)
    except Exception as e:
        print("ERROR: " + filename + ":0: " + str(e))
        return 1
    tool.format_includes()
    first_difference = tool.get_first_differing_original_index()
    if first_difference is not None:
        print(f"ERROR: {filename}:{first_difference + 1}: "
              "the #include ordering is incorrect")
        print("note: fix via bazel-bin/tools/lint/clang-format-includes "
              + filename)
        print("note: if that program does not exist, "
              "you might need to compile it first: "
              "bazel build //tools/lint/...")
        return 1
    return 0


def _check_shebang(filename, disallow_executable):
    """Returns 0 if the filename's executable bit is consistent with the
    presence of a shebang line and the shebang line is in the whitelist of
    acceptable shebang lines, and 1 otherwise.

    If the string "# noqa: shebang" is present in the file, then this check
    will be ignored.
    """
    with open(filename, mode='r', encoding='utf8') as file:
        content = file.read()
    if "# noqa: shebang" in content:
        # Ignore.
        return 0

    is_executable = os.access(filename, os.X_OK)
    if is_executable and disallow_executable:
        print("ERROR: {} is executable, but should not be".format(filename))
        print("note: fix via chmod a-x '{}'".format(filename))
        # Removed for NativeLink Remote Execution
        # return 1

    lines = content.splitlines()
    assert len(lines) > 0, f"Empty file? {filename}"
    shebang = lines[0]
    has_shebang = shebang.startswith("#!")
    if is_executable and not has_shebang:
        print("ERROR: {} is executable but lacks a shebang".format(filename))
        print("note: fix via chmod a-x '{}'".format(filename))
        # Removed for NativeLink Remote Execution
        # return 1
    if has_shebang and not is_executable:
        print("ERROR: {} has a shebang but is not executable".format(filename))
        print("note: fix by removing the first line of the file")
        return 1
    shebang_whitelist = {
        "bash": "#!/bin/bash",
        "python": "#!/usr/bin/env python3",
    }
    if has_shebang and shebang not in list(shebang_whitelist.values()):
        print(("ERROR: shebang '{}' in the file '{}' is not in the shebang "
              "whitelist").format(shebang, filename))
        for hint, replacement_shebang in shebang_whitelist.items():
            if hint in shebang:
                print(("note: fix by replacing the shebang with "
                      "'{}'").format(replacement_shebang))
        return 1
    return 0


def _check_iostream(filename):
    """Forbids using <iostream> unless you're using std::cout or similar.

    See https://en.cppreference.com/w/cpp/header/iostream. Including <iostream>
    inserts non-trivial global constructors and destructors into our code,
    which violates the Google Style Guide.
    """
    # Checks if we're using <iostream>.
    with open(filename, mode='r', encoding='utf-8') as file:
        lines = file.readlines()
    line_num = None
    for i, line in enumerate(lines):
        if line.startswith("#include <iostream>"):
            line_num = i
            break
    if line_num is None:
        return 0

    # We're using <iostream>. Check if it's necessary.
    stream_constant = re.compile("std::c(in|out|err)")
    for line in lines:
        if stream_constant.search(line) is not None:
            # It's necessary.
            return 0

    # It's unnecessary.
    print(f"ERROR: {filename}:{line_num + 1}: "
          "Do not include <iostream> unless you need std::cin, std::cout, or "
          "std::cerr. If you need std::ostream then include <ostream>, or "
          "likewise for std::istream; for std::ifstream include <fstream>. "
          "If no streams are needed, remove the include statement entirely.")
    return 1


def _check_clang_format_toggles(filename):
    """Checks that clang-format-{off,on} are correctly paired up.
    """
    # These are the needles we'll be looking for.
    offs = [
        "// clang-format off\n",
        "// clang-format off ",
        "/* clang-format off */",
        "/* clang-format off to disable clang-format-includes */"
    ]
    ons = [
        "// clang-format on\n",
        "// clang-format on ",
        "/* clang-format on */",
    ]

    with open(filename, mode='r', encoding='utf-8') as file:
        lines = file.readlines()
    enabled = True
    num_errors = 0
    for i, line in enumerate(lines):
        line = line + "\n"
        found_on = any([x in line for x in ons])
        found_off = any([x in line for x in offs])
        if found_on:
            if enabled:
                print(f"ERROR: {filename}:{i + 1}: "
                      "This line is redundant; clang-format is already on")
                num_errors += 1
            enabled = True
        if found_off:
            if not enabled:
                print(f"ERROR: {filename}:{i + 1}: "
                      "This line is redundant; clang-format is already off")
                num_errors += 1
            enabled = False

    return num_errors


def main():
    """Run Drake lint checks on each path specified as a command-line argument.
    Exit 1 if any of the paths are invalid or any lint checks fail.
    Otherwise exit 0.
    """
    total_errors = 0
    if len(sys.argv) > 1 and sys.argv[1] == "--disallow_executable":
        disallow_executable = True
        filenames = sys.argv[2:]
    else:
        disallow_executable = False
        filenames = sys.argv[1:]
    for filename in filenames:
        print("drakelint.py: Linting " + filename)
        total_errors += _check_invalid_line_endings(filename)
        if not filename.endswith((".cc", ".cpp", ".h")):
            # TODO(jwnimmer-tri) We should enable this check for C++ files
            # also, but that runs into some struggle with genfiles.
            total_errors += _check_shebang(filename, disallow_executable)
        if not filename.endswith(".py"):
            total_errors += _check_includes(filename)
            total_errors += _check_unguarded_openmp_uses(filename)
            total_errors += _check_iostream(filename)
            total_errors += _check_clang_format_toggles(filename)
            total_errors += _check_header_using_overloaded(filename)
            total_errors += _check_header_doxygen_file_spelling(filename)

    if total_errors == 0:
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    main()
