package unwrap

import (
	_ "embed"
	"slices"
	"strings"

	"go.yaml.in/yaml/v3"
)

// Program is how a program reads its arguments; programs.yaml describes the
// fields.
type Program struct {
	Values        []string            `yaml:"values"`
	Options       map[string]string   `yaml:"options"`
	Except        map[string][]string `yaml:"except"`
	Choices       map[string][]string `yaml:"choices"`
	Operands      string              `yaml:"operands"`
	EndsOptions   bool                `yaml:"ends_options"`
	Runs          *Runs               `yaml:"runs"`
	Acts          []string            `yaml:"acts"`
	Closed        bool                `yaml:"closed"`
	Flags         []string            `yaml:"flags"`
	Declines      []string            `yaml:"declines"`
	Subcommands   map[string]*Program `yaml:"subcommands"`
	Builtins      []string            `yaml:"builtins"`
	ReadsSettings []string            `yaml:"reads_settings"`
}

// Runs is how a program that runs a command finds it.
type Runs struct {
	Positionals int      `yaml:"positionals"`
	Lookups     []string `yaml:"lookups"`
	Assignments bool     `yaml:"assignments"`
	Input       *Input   `yaml:"input"`
	// KeepsLauncher: rules name the command with the launcher, so the guards
	// do not peel it away.
	KeepsLauncher bool `yaml:"keeps_launcher"`
}

// Input is how xargs adds its input to the command it runs.
type Input struct {
	Replace       []string `yaml:"replace"`
	ReplaceBraces []string `yaml:"replace_braces"`
}

//go:embed programs.yaml
var programsFile []byte

var programs = func() map[string]*Program {
	var found map[string]*Program
	if err := yaml.Unmarshal(programsFile, &found); err != nil {
		panic("programs.yaml: " + err.Error())
	}
	return found
}()

// grammar is how the program a command names reads its arguments, or nil.
func grammar(program string) *Program { return programs[Basename(program)] }

// role is what an option's value is, or "" when it takes none or no role is
// listed; a key ending in `*` names every option it begins.
func (p *Program) role(option string) string {
	if role, ok := p.Options[option]; ok {
		return role
	}
	for key, role := range p.Options {
		if prefix, ok := strings.CutSuffix(key, "*"); ok && strings.HasPrefix(option, prefix) {
			return role
		}
	}
	return ""
}

// takesValue is whether an option consumes the next word.
func (p *Program) takesValue(option string) bool {
	replaces := p.Runs != nil && p.Runs.Input != nil && slices.Contains(p.Runs.Input.Replace, option)
	return replaces || slices.Contains(p.Values, option) || p.role(option) != ""
}

// joinsValue is whether a short option takes a value only when joined to it,
// as xargs -i[R] does.
func (p *Program) joinsValue(option string) bool {
	braces := p.Runs != nil && p.Runs.Input != nil && slices.Contains(p.Runs.Input.ReplaceBraces, option)
	return braces && !strings.HasPrefix(option, "--")
}

// known is whether a closed program lists the option.
func (p *Program) known(option string) bool {
	return p.takesValue(option) || slices.Contains(p.Flags, option)
}

// Option is one option of a command as its program reads it.
type Option struct {
	Name  string // without a joined value: `--kill-after` from `--kill-after=5`
	Value string // its value, joined or the next word, when it takes one
	Takes bool   // it takes a value
	At    int    // the index of the option's word
	// ValueAt is the index of the word holding its value: At when joined.
	ValueAt int
}

// Parsed is a command's words as its program reads them. Every index is into
// the words given to parse.
type Parsed struct {
	Grammar    *Program // the grammar that read the last arguments: a subcommand's, or the program's
	Options    []Option
	Operands   []int // before any command it runs
	Subcommand int   // the subcommand's word, or -1
	Command    int   // where the command it runs starts, or -1
	Lookup     bool  // an option reports on the command instead of running it
	End        int   // a literal `--` that ends its options, or -1
}

// parse reads words, a command whose first word is the program, with the
// program's grammar, or returns nil when there is none. An option is a word
// that starts with `-` before a `--`; a short option that takes a value may
// carry it joined (`-k5`), a long one after `=`.
func parse(words []string) *Parsed {
	if len(words) == 0 {
		return nil
	}
	program := grammar(words[0])
	if program == nil {
		return nil
	}
	parsed := &Parsed{Grammar: program, Subcommand: -1, Command: -1, End: -1}
	positionals := 0
	if program.Runs != nil {
		positionals = program.Runs.Positionals
	}
	for index := 1; index < len(words); index++ {
		word := words[index]
		current := parsed.Grammar
		switch {
		case word == "--" && parsed.End == -1:
			parsed.End = index
		case parsed.End == -1 && strings.HasPrefix(word, "-") && word != "-":
			option := Option{Name: word, At: index, ValueAt: index}
			if name, value, joined := strings.Cut(word, "="); joined && strings.HasPrefix(word, "--") {
				option.Name, option.Value, option.Takes = name, value, true
			} else if !strings.HasPrefix(word, "--") && !current.takesValue(word) {
				// A bundle of short flags (`-ne`): the first that takes a
				// value takes the rest of the word, or the next word, and
				// one with an optional value (xargs -i[R]) the rest of the
				// word. The flags before it are options of their own.
				var flags []Option
				for letter := 1; letter < len(word); letter++ {
					short, rest := "-"+word[letter:letter+1], word[letter+1:]
					if current.takesValue(short) || current.joinsValue(short) {
						option.Name, option.Value, option.Takes = short, rest, current.takesValue(short) || rest != ""
						parsed.Options = append(parsed.Options, flags...)
						break
					}
					flags = append(flags, Option{Name: short, At: index, ValueAt: index})
				}
			}
			bundleEnds := option.Takes && option.Value == "" && option.Name != word && !strings.HasPrefix(word, "--")
			if !option.Takes && current.takesValue(option.Name) || bundleEnds {
				if index+1 < len(words) {
					index++
					option.Value, option.Takes, option.ValueAt = words[index], true, index
				}
			}
			parsed.Options = append(parsed.Options, option)
			if current.Runs != nil && slices.Contains(current.Runs.Lookups, option.Name) {
				parsed.Lookup = true
				return parsed
			}
		case current.Runs != nil && current.Runs.Assignments && parsed.End == -1 && assignment.MatchString(word):
			parsed.Operands = append(parsed.Operands, index)
		case parsed.Subcommand == -1 && (current.Subcommands != nil || current.Builtins != nil):
			parsed.Subcommand = index
			sub := current.Subcommands[word]
			if sub == nil {
				// The subcommand reads the rest by a grammar not listed.
				for rest := index + 1; rest < len(words); rest++ {
					parsed.Operands = append(parsed.Operands, rest)
				}
				return parsed
			}
			parsed.Grammar = sub
			if sub.Runs != nil {
				positionals = sub.Runs.Positionals
			}
		case current.Runs != nil && positionals > 0:
			positionals--
			parsed.Operands = append(parsed.Operands, index)
		case current.Runs != nil:
			parsed.Command = index
			return parsed
		default:
			parsed.Operands = append(parsed.Operands, index)
		}
	}
	return parsed
}
