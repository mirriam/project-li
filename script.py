<?php
/*
Plugin Name: Job Scraper Selector
Description: Allows selecting country and specialty to trigger LinkedIn scraper via GitHub Actions.
Version: 1.4
Author: Your Name
*/

// Add admin menu
add_action('admin_menu', 'jss_add_admin_page');
function jss_add_admin_page() {
    add_menu_page('Job Scraper Selector', 'Scraper Selector', 'manage_options', 'jss-selector', 'jss_admin_page_content', 'dashicons-admin-tools');
    add_submenu_page('jss-selector', 'Scraper Settings', 'Settings', 'manage_options', 'jss-settings', 'jss_settings_page_content');
}

// Settings page
add_action('admin_init', 'jss_register_settings');
function jss_register_settings() {
    register_setting('jss_settings_group', 'jss_gh_pat', array('sanitize_callback' => 'sanitize_text_field'));
    register_setting('jss_settings_group', 'jss_github_repo', array('sanitize_callback' => 'sanitize_text_field'));
}

function jss_settings_page_content() {
    ?>
    <div class="wrap">
        <h1>Job Scraper Settings</h1>
        <form method="post" action="options.php">
            <?php
            settings_fields('jss_settings_group');
            do_settings_sections('jss_settings_group');
            ?>
            <table class="form-table">
                <tr>
                    <th scope="row"><label for="jss_gh_pat">GitHub Personal Access Token</label></th>
                    <td>
                        <input type="password" id="jss_gh_pat" name="jss_gh_pat" value="<?php echo esc_attr(get_option('jss_gh_pat')); ?>" class="regular-text">
                        <p class="description">Enter your GitHub PAT with 'repo' and 'workflow' scopes.</p>
                    </td>
                </tr>
                <tr>
                    <th scope="row"><label for="jss_github_repo">GitHub Repository</label></th>
                    <td>
                        <input type="text" id="jss_github_repo" name="jss_github_repo" value="<?php echo esc_attr(get_option('jss_github_repo', 'yourusername/project-li')); ?>" class="regular-text">
                        <p class="description">Enter your repository in the format 'username/project-li'.</p>
                    </td>
                </tr>
            </table>
            <?php submit_button(); ?>
        </form>
    </div>
    <?php
}

// Main selector page
function jss_admin_page_content() {
    if (isset($_POST['jss_submit'])) {
        $country = sanitize_text_field($_POST['country']);
        $specialty = sanitize_text_field($_POST['specialty']);
        $github_pat = get_option('jss_gh_pat');
        $github_repo = get_option('jss_github_repo', 'yourusername/project-li');
        $workflow_id = 'scraper.yml';

        if (empty($github_pat)) {
            echo '<div class="notice notice-error"><p>Please configure GitHub PAT in Settings.</p></div>';
        } elseif (empty($github_repo) || strpos($github_repo, '/') === false) {
            echo '<div class="notice notice-error"><p>Please configure a valid GitHub repository in Settings (format: username/project-li).</p></div>';
        } else {
            $data = array(
                'ref' => 'main',
                'inputs' => array(
                    'country' => $country,
                    'specialty' => $specialty
                )
            );

            $ch = curl_init("https://api.github.com/repos/$github_repo/actions/workflows/$workflow_id/dispatches");
            curl_setopt($ch, CURLOPT_RETURNTRANSFER, true);
            curl_setopt($ch, CURLOPT_POST, true);
            curl_setopt($ch, CURLOPT_POSTFIELDS, json_encode($data));
            curl_setopt($ch, CURLOPT_HTTPHEADER, array(
                'Authorization: token ' . $github_pat,
                'Accept: application/vnd.github.v3+json',
                'User-Agent: WP-Plugin'
            ));
            $response = curl_exec($ch);
            $http_code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
            $error = curl_error($ch);
            curl_close($ch);

            if ($http_code == 204) {
                echo '<div class="notice notice-success"><p>Workflow dispatched for ' . esc_html($country) . ' - ' . esc_html($specialty) . '</p></div>';
            } else {
                echo '<div class="notice notice-error"><p>Failed to dispatch workflow. HTTP ' . esc_html($http_code) . ': ' . esc_html($response) . (empty($error) ? '' : ' cURL Error: ' . esc_html($error)) . '</p></div>';
            }
        }
    }

    // List of countries (can be expanded)
    $countries = array('Mauritius', 'France', 'United States', 'United Kingdom', 'India', 'South Africa');
    ?>
    <div class="wrap">
        <h1>Job Scraper Selector</h1>
        <form method="post">
            <table class="form-table">
                <tr>
                    <th scope="row"><label for="country">Country</label></th>
                    <td>
                        <select id="country" name="country">
                            <?php foreach ($countries as $country): ?>
                                <option value="<?php echo esc_attr($country); ?>" <?php selected($country, 'Mauritius'); ?>>
                                    <?php echo esc_html($country); ?>
                                </option>
                            <?php endforeach; ?>
                        </select>
                    </td>
                </tr>
                <tr>
                    <th scope="row"><label for="specialty">Specialty/Keywords</label></th>
                    <td><input type="text" id="specialty" name="specialty" class="regular-text" placeholder="e.g., software engineer"></td>
                </tr>
            </table>
            <?php submit_button('Run Scraper', 'primary', 'jss_submit'); ?>
        </form>
    </div>
    <?php
}
?>
