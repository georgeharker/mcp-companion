{
  description = "An MCP proxy bridge that aggregates multiple Model Context Protocol servers behind a single HTTP endpoint";

  inputs = {
    devshell = {
      url = "github:numtide/devshell";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    flake-parts = {
      url = "github:hercules-ci/flake-parts";
      inputs.nixpkgs-lib.follows = "nixpkgs";
    };
    nixpkgs.url = "github:NixOS/nixpkgs/nixpkgs-unstable";
    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs = {
        pyproject-nix.follows = "pyproject-nix";
        uv2nix.follows = "uv2nix";
        nixpkgs.follows = "nixpkgs";
      };
    };
    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    treefmt-nix = {
      url = "github:numtide/treefmt-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs = {
        pyproject-nix.follows = "pyproject-nix";
        nixpkgs.follows = "nixpkgs";
      };
    };
  };

  outputs = inputs:
    inputs.flake-parts.lib.mkFlake {inherit inputs;} {
      systems = [
        "x86_64-linux"
        "aarch64-linux"
        "x86_64-darwin"
        "aarch64-darwin"
      ];
      imports = with inputs; [
        devshell.flakeModule
        flake-parts.flakeModules.easyOverlay
        treefmt-nix.flakeModule
      ];
      perSystem = {
        config,
        lib,
        pkgs,
        ...
      }: let
        inherit (pkgs.callPackages inputs.pyproject-nix.build.util {}) mkApplication;
        workspace = inputs.uv2nix.lib.workspace.loadWorkspace {workspaceRoot = ./combiner;};
        overlay = workspace.mkPyprojectOverlay {
          sourcePreference = "wheel";
        };
        editableOverlay = workspace.mkEditablePyprojectOverlay {
          root = "$ROOT_REPO/combiner";
        };
        python = lib.head (
          inputs.pyproject-nix.lib.util.filterPythonInterpreters {
            inherit (workspace) requires-python;
            inherit (pkgs) pythonInterpreters;
          }
        );
        pythonBase = pkgs.callPackage inputs.pyproject-nix.build.packages {
          inherit python;
        };
        pythonSet = pythonBase.overrideScope (
          lib.composeManyExtensions [
            inputs.pyproject-build-systems.overlays.wheel
            overlay
          ]
        );
        editablePythonSet = pythonSet.overrideScope (
          lib.composeManyExtensions [
            editableOverlay
            (_final: prev: {
              mcp-combiner = prev.mcp-combiner.overrideAttrs (old: {
                nativeBuildInputs =
                  (old.nativeBuildInputs or [])
                  ++ [
                    prev.editables
                  ];
              });
            })
          ]
        );
        virtualenv = editablePythonSet.mkVirtualEnv "mpc-combiner-dev-env" workspace.deps.all;
        virtualenv-test = pythonSet.mkVirtualEnv "mcp-combiner-test-env" workspace.deps.all;
        mcp-combiner = pythonSet.mkVirtualEnv "mcp-combiner-env" workspace.deps.default;
        mcp-combiner-bin = mkApplication {
          venv = mcp-combiner;
          package = pythonSet.mcp-combiner;
        };
        mcp-companion-nvim = pkgs.vimUtils.buildVimPlugin {
          pname = "mcp-companion-nvim";
          version = toString (
            inputs.self.shortRev or inputs.self.dirtyShortRev or inputs.self.lastModified or "git"
          );
          src = with lib.fileset;
            toSource {
              root = ./.;
              fileset = unions (
                map maybeMissing [
                  ./lua
                  ./plugin
                  ./ftdetect
                  ./doc
                ]
              );
            };
        };
      in {
        checks = {
          tests = pkgs.stdenv.mkDerivation {
            name = "mcp-combiner-tests";
            src = ./combiner;
            nativeBuildInputs = [virtualenv-test];
            buildPhase = ''
              pytest -v tests/
              mypy --strict mcp_combiner/ tests/
            '';
            installPhase = ''
              touch $out
            '';
          };
        };
        devshells.default = {
          env = [
            {
              name = "UV_NO_SYNC";
              value = "1";
            }
            {
              name = "UV_PYTHON_DOWNLOADS";
              value = "never";
            }
            {
              name = "UV_PYTHON";
              value = editablePythonSet.python.interpreter;
            }
          ];
          devshell.startup.python-env.text = ''
            unset PYTHONPATH
            export REPO_ROOT="$(git rev-parse --show-toplevel)/combiner"
          '';
          packages = with pkgs; [
            uv
            virtualenv
          ];
        };
        packages = {
          inherit mcp-combiner mcp-combiner-bin mcp-companion-nvim;
          default = mcp-combiner-bin;
        };
        overlayAttrs = config.packages;
        treefmt = {
          programs = {
            nixfmt.enable = true;
            ruff-check.enable = true;
            ruff-format.enable = true;
            statix.enable = true;
            stylua.enable = true;
          };
        };
      };
    };
}
