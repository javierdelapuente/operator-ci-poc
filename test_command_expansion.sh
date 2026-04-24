#!/bin/bash

# Test to understand the expansion behavior

# Scenario: root shell runs this, MODULE is set
export SPREAD_PATH="/some/path"
export MODULE="test_foo"

# This is what the code generates
echo "=== Command as written in task.yaml ==="
cat << 'EOFYAML'
execute: |
    loginctl enable-linger ubuntu
    cd "${SPREAD_PATH}"
    runuser -l ubuntu -c "cd \"${SPREAD_PATH}\" && $(opcli pytest expand -- -k \"$MODULE\")"
EOFYAML

echo ""
echo "=== Expansion order ==="
echo "1. \${SPREAD_PATH} expands in root shell (before runuser): $SPREAD_PATH"
echo "2. \$MODULE is inside \$(opcli ...) which runs in root shell: $MODULE"
echo "3. Command substitution \$(opcli pytest expand -- -k \"$MODULE\") runs as root"
echo "   - opcli sees: pytest expand -- -k \"test_foo\""
echo "   - opcli outputs: some-command-string"
echo "4. After command substitution, runuser receives:"
echo "   runuser -l ubuntu -c \"cd \"/some/path\" && some-command-string\""
echo ""

echo "=== Problem Analysis ==="
echo "The issue: opcli is designed to OUTPUT a command to be executed."
echo "But \$(opcli pytest expand -- -k \"$MODULE\") runs opcli and captures its output."
echo "Then that output is passed as the -c argument to runuser."
echo ""
echo "If opcli outputs: 'tox -e integration -- -k test_foo'"
echo "The final command becomes:"
echo "runuser -l ubuntu -c \"cd \"/some/path\" && tox -e integration -- -k test_foo\""
echo ""
echo "This is CORRECT if opcli is meant to generate the command."
