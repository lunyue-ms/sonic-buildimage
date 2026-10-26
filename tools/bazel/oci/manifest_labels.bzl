"""Make-generated SONiC manifests as explicit OCI label inputs."""

def _manifest_labels_impl(ctx):
    out = ctx.actions.declare_file(ctx.label.name + ".labels")
    args = ctx.actions.args()
    args.add("--src", ctx.file.src)
    args.add("--out", out)
    ctx.actions.run(
        inputs = [ctx.file.src],
        outputs = [out],
        executable = ctx.executable._converter,
        arguments = [args],
        mnemonic = "SonicManifestLabels",
    )
    return [DefaultInfo(files = depset([out]))]

manifest_labels = rule(
    implementation = _manifest_labels_impl,
    attrs = {
        "src": attr.label(allow_single_file = [".json"], mandatory = True),
        "_converter": attr.label(
            default = Label("//tools/bazel/oci:manifest_to_labels"),
            executable = True,
            cfg = "exec",
        ),
    },
)
