*demoterm* makes your CLI demos more visual by displaying what you type.

# Installation

```
$ pip install demoterm
```

# Usage

```
$ demoterm
```

You will probably want to combine `demoterm` with a screen recording tool
such as `asciinema`.

The program will run the shell specified by the `SHELL` environment variable by default, and if this variable is missing it will try to find `bash` or `sh`. Alternatively, you can specify the shell you want as an argument (e.g., `demoterm zsh`).
