<?php
declare(strict_types=1);

// Creates a local distribution without Composer or registry access.
if (!class_exists(ZipArchive::class)) {
    fwrite(STDERR, "Packaging requires PHP's zip extension.\n");
    exit(1);
}
$manifest = json_decode(file_get_contents(__DIR__ . '/composer.json'), flags: JSON_THROW_ON_ERROR);
$directory = __DIR__ . '/dist';
if (!is_dir($directory) && !mkdir($directory, 0777, true)) {
    throw new RuntimeException('Could not create the package directory.');
}
$destination = $directory . '/tinyagent-sdk-' . $manifest->version . '.zip';
$archive = new ZipArchive();
if ($archive->open($destination, ZipArchive::CREATE | ZipArchive::OVERWRITE) !== true) {
    throw new RuntimeException('Could not open the package archive.');
}
try {
    $files = ['autoload.php', 'composer.json', 'README.md', 'LICENSE'];
    foreach (glob(__DIR__ . '/src/*.php') as $path) {
        $files[] = 'src/' . basename($path);
    }
    foreach (glob(__DIR__ . '/resources/draft202012/*') as $path) {
        $files[] = 'resources/draft202012/' . basename($path);
    }
    foreach ($files as $file) {
        if (!$archive->addFile(__DIR__ . '/' . $file, $file)) {
            throw new RuntimeException("Could not package {$file}.");
        }
    }
} finally {
    if (!$archive->close()) {
        throw new RuntimeException('Could not finish the package archive.');
    }
}
echo "Created {$destination}\n";
