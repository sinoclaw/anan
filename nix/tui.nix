# nix/tui.nix — Hermes TUI (Ink/React) compiled with tsc and bundled
{ pkgs, hermesNpmLib, ... }:
let
  src = ../ui-tui;
  npmDeps = pkgs.fetchNpmDeps {
    inherit src;
    hash = "sha256-JuwShoVDzys7W350o4YQECWflOEsx2zLKlJq+zgGi7A=";
  };

  npm = hermesNpmLib.mkNpmPassthru { folder = "ui-tui"; attr = "tui"; pname = "anan-tui"; };

  packageJson = builtins.fromJSON (builtins.readFile (src + "/package.json"));
  version = packageJson.version;
in
pkgs.buildNpmPackage (npm // {
  pname = "anan-tui";
  inherit src npmDeps version;

  doCheck = false;
  npmFlags = [ "--legacy-peer-deps" ];

  installPhase = ''
    runHook preInstall

    mkdir -p $out/lib/anan-tui

    cp -r dist $out/lib/anan-tui/dist

    # runtime node_modules
    cp -r node_modules $out/lib/anan-tui/node_modules

    # @anan/ink is a file: dependency, we need to copy it in fr
    rm -f $out/lib/anan-tui/node_modules/@anan/ink
    cp -r packages/anan-ink $out/lib/anan-tui/node_modules/@anan/ink

    # package.json needed for "type": "module" resolution
    cp package.json $out/lib/anan-tui/

    runHook postInstall
  '';
})
