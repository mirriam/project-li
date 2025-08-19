<?php
/**
 * Plugin Name: Scraped Data Staging
 * Plugin URI: https://example.com/scraped-data-staging
 * Description: A WordPress plugin to stage scraped company and job data from a GitHub-hosted scraper script in a single post type for review before publishing. Settings are managed under the plugin menu, manual post creation is disabled, and GitHub authentication uses only a personal access token with a hidden repository. Displays scraper results in a table after running.
 * Version: 1.6.6
 * Author: Grok
 * Author URI: https://x.ai
 * License: GPL-2.0+
 * Text Domain: scraped-data-staging
 */

if ( ! defined( 'ABSPATH' ) ) {
    exit; // Prevent direct access.
}

/**
 * Class to manage the Scraped Data Staging plugin.
 */
class Scraped_Data_Staging {
    /**
     * Hardcoded GitHub repository.
     */
    private static $github_repo = 'mirriam/project-li';

    /**
     * Initialize the plugin.
     */
    public static function init() {
        add_action( 'init', array( __CLASS__, 'register_post_type' ) );
        add_action( 'init', array( __CLASS__, 'register_meta' ) );
        add_action( 'add_meta_boxes', array( __CLASS__, 'add_approve_meta_box' ) );
        add_action( 'save_post', array( __CLASS__, 'handle_approval' ) );
        add_filter( 'manage_staging_scraped_posts_columns', array( __CLASS__, 'add_admin_columns' ) );
        add_action( 'manage_staging_scraped_posts_custom_column', array( __CLASS__, 'populate_admin_columns' ), 10, 2 );
        add_action( 'admin_menu', array( __CLASS__, 'add_settings_page' ) );
        add_action( 'admin_init', array( __CLASS__, 'register_settings' ) );
        add_action( 'admin_post_sds_trigger_scraper', array( __CLASS__, 'trigger_scraper' ) );
        add_action( 'admin_post_sds_refresh_results', array( __CLASS__, 'refresh_results' ) );
    }

    /**
     * Register the unified staging post type.
     */
    public static function register_post_type() {
        register_post_type( 'staging_scraped', array(
            'labels' => array(
                'name' => __( 'Staging Scraped Data', 'scraped-data-staging' ),
                'singular_name' => __( 'Staging Scraped Item', 'scraped-data-staging' ),
                'menu_name' => __( 'Staging Scraped Data', 'scraped-data-staging' ),
                'all_items' => __( 'All Scraped Data', 'scraped-data-staging' ),
                'edit_item' => __( 'Edit Scraped Item', 'scraped-data-staging' ),
                'view_item' => __( 'View Scraped Item', 'scraped-data-staging' ),
                'search_items' => __( 'Search Scraped Data', 'scraped-data-staging' ),
            ),
            'public' => false,
            'show_ui' => true,
            'show_in_menu' => true,
            'supports' => array( 'title', 'editor', 'thumbnail' ),
            'show_in_rest' => true,
            'rest_base' => 'staging-scraped',
            'capability_type' => 'post',
            'map_meta_cap' => true,
            'capabilities' => array(
                'create_posts' => 'do_not_allow',
            ),
            'taxonomies' => array( 'job_listing_type', 'job_listing_region' ),
            'menu_icon' => 'dashicons-database-import',
        ) );
    }

    /**
     * Register meta fields for the staging post type.
     */
    public static function register_meta() {
        $meta_keys = self::get_all_meta_keys();
        foreach ( $meta_keys as $key ) {
            register_post_meta( 'staging_scraped', $key, array(
                'type' => 'string',
                'single' => true,
                'show_in_rest' => true,
                'sanitize_callback' => 'sanitize_text_field',
                'auth_callback' => function() {
                    return current_user_can( 'edit_posts' );
                },
            ) );
        }
    }

    /**
     * Get all unique meta keys for company and job.
     */
    private static function get_all_meta_keys() {
        return array(
            '_scraped_type',
            '_company_name',
            '_company_logo',
            '_company_website',
            '_company_industry',
            '_company_founded',
            '_company_type',
            '_company_address',
            '_company_tagline',
            '_company_twitter',
            '_company_video',
            '_job_title',
            '_job_location',
            '_job_type',
            '_job_description',
            '_job_salary',
            '_application',
            '_company_id',
        );
    }

    /**
     * Add custom admin columns.
     */
    public static function add_admin_columns( $columns ) {
        $new_columns = array();
        foreach ( $columns as $key => $value ) {
            $new_columns[ $key ] = $value;
            if ( $key === 'title' ) {
                $new_columns['scraped_type'] = __( 'Type', 'scraped-data-staging' );
                $new_columns['company_name'] = __( 'Company Name', 'scraped-data-staging' );
                $new_columns['job_title'] = __( 'Job Title', 'scraped-data-staging' );
            }
        }
        return $new_columns;
    }

    /**
     * Populate custom admin columns.
     */
    public static function populate_admin_columns( $column, $post_id ) {
        switch ( $column ) {
            case 'scraped_type':
                echo esc_html( get_post_meta( $post_id, '_scraped_type', true ) );
                break;
            case 'company_name':
                echo esc_html( get_post_meta( $post_id, '_company_name', true ) );
                break;
            case 'job_title':
                echo esc_html( get_post_meta( $post_id, '_job_title', true ) );
                break;
        }
    }

    /**
     * Add meta box for approving staging posts.
     */
    public static function add_approve_meta_box() {
        add_meta_box(
            'sds_approve_box',
            __( 'Approve Staging Data', 'scraped-data-staging' ),
            array( __CLASS__, 'approve_meta_box_callback' ),
            'staging_scraped',
            'side',
            'high'
        );
    }

    /**
     * Render meta box content.
     */
    public static function approve_meta_box_callback( $post ) {
        wp_nonce_field( 'sds_approve_nonce', 'sds_approve_nonce' );
        ?>
        <p>
            <input type="submit" name="sds_approve" value="<?php esc_attr_e( 'Approve and Publish', 'scraped-data-staging' ); ?>" class="button button-primary" />
        </p>
        <?php
    }

    /**
     * Handle approval: Copy staging post to main post type and delete staging post.
     */
    public static function handle_approval( $post_id ) {
        if ( defined( 'DOING_AUTOSAVE' ) && DOING_AUTOSAVE ) {
            return;
        }
        if ( ! isset( $_POST['sds_approve_nonce'] ) || ! wp_verify_nonce( $_POST['sds_approve_nonce'], 'sds_approve_nonce' ) ) {
            return;
        }
        if ( ! current_user_can( 'edit_post', $post_id ) ) {
            return;
        }
        if ( ! isset( $_POST['sds_approve'] ) ) {
            return;
        }

        $scraped_type = get_post_meta( $post_id, '_scraped_type', true );
        if ( ! in_array( $scraped_type, array( 'company', 'job' ) ) ) {
            wp_die( __( 'Invalid scraped type.', 'scraped-data-staging' ) );
        }

        $target_post_type = ( $scraped_type === 'company' ) ? 'company' : 'job_listing';

        $post_data = array(
            'post_title'   => get_the_title( $post_id ),
            'post_content' => get_post_field( 'post_content', $post_id ),
            'post_status'  => 'publish',
            'post_type'    => $target_post_type,
        );

        $new_post_id = wp_insert_post( $post_data );

        if ( ! is_wp_error( $new_post_id ) ) {
            $meta_keys = self::get_all_meta_keys();
            foreach ( $meta_keys as $key ) {
                if ( $key === '_scraped_type' ) {
                    continue;
                }
                $value = get_post_meta( $post_id, $key, true );
                if ( $value !== '' ) {
                    update_post_meta( $new_post_id, $key, $value );
                }
            }

            $featured_image = get_post_thumbnail_id( $post_id );
            if ( $featured_image ) {
                set_post_thumbnail( $new_post_id, $featured_image );
            }

            if ( $scraped_type === 'job' ) {
                $taxonomies = array( 'job_listing_type', 'job_listing_region' );
                foreach ( $taxonomies as $tax ) {
                    $terms = wp_get_object_terms( $post_id, $tax, array( 'fields' => 'ids' ) );
                    if ( ! is_wp_error( $terms ) && ! empty( $terms ) ) {
                        wp_set_object_terms( $new_post_id, $terms, $tax );
                    }
                }
            }

            wp_delete_post( $post_id, true );
            wp_redirect( get_edit_post_link( $new_post_id, 'raw' ) );
            exit;
        } else {
            wp_die( __( 'Error creating main post.', 'scraped-data-staging' ) );
        }
    }

    /**
     * Remove "Add New" link from post row actions.
     */
    public static function remove_row_actions( $actions, $post ) {
        if ( $post->post_type === 'staging_scraped' ) {
            unset( $actions['inline hide-if-no-js'] );
        }
        return $actions;
    }

    /**
     * Hide the "Add New" button in the admin interface.
     */
    public static function hide_add_new_button() {
        if ( get_current_screen()->post_type === 'staging_scraped' ) {
            echo '<style>
                .page-title-action { display: none !important; }
            </style>';
        }
    }

    /**
     * Add settings page under the plugin menu.
     */
    public static function add_settings_page() {
        add_submenu_page(
            'edit.php?post_type=staging_scraped',
            __( 'Scraper Settings', 'scraped-data-staging' ),
            __( 'Scraper Settings', 'scraped-data-staging' ),
            'manage_options',
            'sds-settings',
            array( __CLASS__, 'settings_page_callback' )
        );
    }

    /**
     * Register settings for GitHub and scraper configuration.
     */
    public static function register_settings() {
        register_setting( 'sds_settings_group', 'sds_github_token', array( 'sanitize_callback' => 'sanitize_text_field' ) );
        register_setting( 'sds_settings_group', 'sds_base_url', array( 'sanitize_callback' => 'esc_url_raw' ) );
        register_setting( 'sds_settings_group', 'sds_wp_username', array( 'sanitize_callback' => 'sanitize_text_field' ) );
        register_setting( 'sds_settings_group', 'sds_wp_app_password', array( 'sanitize_callback' => 'sanitize_text_field' ) );
        register_setting( 'sds_settings_group', 'sds_scrape_location', array( 'sanitize_callback' => 'sanitize_text_field' ) );
        register_setting( 'sds_settings_group', 'sds_last_workflow_run_id', array( 'sanitize_callback' => 'sanitize_text_field' ) );

        add_settings_section( 'sds_general', __( 'GitHub and Scraper Settings', 'scraped-data-staging' ), null, 'sds-settings' );

        add_settings_field(
            'sds_github_token',
            __( 'GitHub Personal Access Token', 'scraped-data-staging' ),
            array( __CLASS__, 'field_callback' ),
            'sds-settings',
            'sds_general',
            array( 'id' => 'sds_github_token', 'type' => 'password', 'description' => __( 'Enter your GitHub personal access token with repo, workflow, and actions permissions.', 'scraped-data-staging' ) )
        );
        add_settings_field(
            'sds_base_url',
            __( 'WordPress Base URL', 'scraped-data-staging' ),
            array( __CLASS__, 'field_callback' ),
            'sds-settings',
            'sds_general',
            array( 'id' => 'sds_base_url', 'type' => 'url', 'description' => __( 'Enter the base URL of your WordPress site (e.g., https://your-site.com).', 'scraped-data-staging' ) )
        );
        add_settings_field(
            'sds_wp_username',
            __( 'WordPress Username', 'scraped-data-staging' ),
            array( __CLASS__, 'field_callback' ),
            'sds-settings',
            'sds_general',
            array( 'id' => 'sds_wp_username', 'type' => 'text', 'description' => __( 'Enter the WordPress username for REST API authentication.', 'scraped-data-staging' ) )
        );
        add_settings_field(
            'sds_wp_app_password',
            __( 'WordPress Application Password', 'scraped-data-staging' ),
            array( __CLASS__, 'field_callback' ),
            'sds-settings',
            'sds_general',
            array( 'id' => 'sds_wp_app_password', 'type' => 'password', 'description' => __( 'Enter the application password for REST API authentication.', 'scraped-data-staging' ) )
        );
        add_settings_field(
            'sds_scrape_location',
            __( 'Scrape Location', 'scraped-data-staging' ),
            array( __CLASS__, 'field_callback' ),
            'sds-settings',
            'sds_general',
            array( 'id' => 'sds_scrape_location', 'type' => 'text', 'description' => __( 'Enter the location for job scraping (e.g., Worldwide, New York).', 'scraped-data-staging' ) )
        );
    }

    /**
     * Render settings field.
     */
    public static function field_callback( $args ) {
        $value = get_option( $args['id'], ( $args['id'] === 'sds_base_url' ? get_site_url() : '' ) );
        ?>
        <input type="<?php echo esc_attr( $args['type'] ); ?>" id="<?php echo esc_attr( $args['id'] ); ?>" name="<?php echo esc_attr( $args['id'] ); ?>" value="<?php echo esc_attr( $value ); ?>" class="regular-text" />
        <?php if ( ! empty( $args['description'] ) ) : ?>
            <p class="description"><?php echo esc_html( $args['description'] ); ?></p>
        <?php endif; ?>
        <?php
    }

    /**
     * Check GitHub connection status.
     */
    private static function check_github_connection() {
        $token = get_option( 'sds_github_token', '' );

        if ( ! $token ) {
            return array( 'status' => 'error', 'message' => __( 'GitHub token is missing.', 'scraped-data-staging' ) );
        }

        $repo_url = "https://api.github.com/repos/" . self::$github_repo;
        $headers = array(
            'Authorization' => 'Bearer ' . $token,
            'Accept' => 'application/vnd.github.v3+json',
            'User-Agent' => 'WordPress-Scraped-Data-Staging',
        );

        $response = wp_remote_get( $repo_url, array( 'headers' => $headers, 'timeout' => 10 ) );

        if ( is_wp_error( $response ) ) {
            return array( 'status' => 'error', 'message' => __( 'Failed to connect to GitHub: ', 'scraped-data-staging' ) . $response->get_error_message() );
        }

        $status_code = wp_remote_retrieve_response_code( $response );
        if ( $status_code === 200 ) {
            return array( 'status' => 'success', 'message' => __( 'Successfully connected to GitHub repository.', 'scraped-data-staging' ) );
        } else {
            $body = json_decode( wp_remote_retrieve_body( $response ), true );
            return array( 'status' => 'error', 'message' => __( 'GitHub connection failed: ', 'scraped-data-staging' ) . ( $body['message'] ?? 'Unknown error' ) );
        }
    }

    /**
     * Trigger the GitHub Actions workflow and fetch initial results.
     */
    public static function trigger_scraper() {
        if ( ! current_user_can( 'manage_options' ) ) {
            wp_die( __( 'Unauthorized access.', 'scraped-data-staging' ) );
        }

        check_admin_referer( 'sds_trigger_scraper_nonce' );

        $token = get_option( 'sds_github_token', '' );
        $wp_username = get_option( 'sds_wp_username', '' );
        $wp_app_password = get_option( 'sds_wp_app_password', '' );
        $scrape_location = get_option( 'sds_scrape_location', '' );
        $wp_base_url = get_option( 'sds_base_url', '' );

        // Validate required fields
        if ( ! $token ) {
            wp_redirect( admin_url( 'edit.php?post_type=staging_scraped&page=sds-settings&error=' . urlencode( __( 'GitHub token is missing.', 'scraped-data-staging' ) ) ) );
            exit;
        }

        if ( ! $wp_username || ! $wp_app_password || ! $scrape_location || ! $wp_base_url ) {
            wp_redirect( admin_url( 'edit.php?post_type=staging_scraped&page=sds-settings&error=' . urlencode( __( 'WordPress username, application password, scrape location, or base URL is missing.', 'scraped-data-staging' ) ) ) );
            exit;
        }

        // Generate a nonce for the REST API
        $nonce = wp_create_nonce( 'wp_rest' );

        $workflow_url = "https://api.github.com/repos/" . self::$github_repo . "/actions/workflows/scraper.yml/dispatches";
        $headers = array(
            'Authorization' => 'Bearer ' . $token,
            'Accept' => 'application/vnd.github.v3+json',
            'User-Agent' => 'WordPress-Scraped-Data-Staging',
            'Content-Type' => 'application/json',
        );

        $body = array(
            'ref' => 'main',
            'inputs' => array(
                'wp_base_url' => $wp_base_url,
                'wp_username' => $wp_username,
                'wp_app_password' => $wp_app_password,
                'scrape_location' => $scrape_location,
                'wp_rest_nonce' => $nonce,
            ),
        );

        $response = wp_remote_post( $workflow_url, array(
            'headers' => $headers,
            'body' => wp_json_encode( $body ),
            'timeout' => 15,
        ) );

        if ( is_wp_error( $response ) ) {
            wp_redirect( admin_url( 'edit.php?post_type=staging_scraped&page=sds-settings&error=' . urlencode( __( 'Failed to trigger scraper: ', 'scraped-data-staging' ) . $response->get_error_message() ) ) );
            exit;
        }

        $status_code = wp_remote_retrieve_response_code( $response );
        if ( $status_code === 204 ) {
            // Fetch the latest workflow run to get the run ID
            $runs_url = "https://api.github.com/repos/" . self::$github_repo . "/actions/workflows/scraper.yml/runs";
            $runs_response = wp_remote_get( $runs_url, array(
                'headers' => $headers,
                'timeout' => 10,
            ) );

            if ( ! is_wp_error( $runs_response ) && wp_remote_retrieve_response_code( $runs_response ) === 200 ) {
                $runs = json_decode( wp_remote_retrieve_body( $runs_response ), true );
                if ( ! empty( $runs['workflow_runs'] ) ) {
                    $latest_run = $runs['workflow_runs'][0];
                    update_option( 'sds_last_workflow_run_id', $latest_run['id'] );
                }
            }

            // Fetch initial results
            $results = self::get_workflow_results();
            if ( $results['status'] === 'success' ) {
                wp_redirect( admin_url( 'edit.php?post_type=staging_scraped&page=sds-settings&success=' . urlencode( __( 'Scraper workflow triggered successfully.', 'scraped-data-staging' ) ) . '&results_refreshed=1' ) );
            } else {
                wp_redirect( admin_url( 'edit.php?post_type=staging_scraped&page=sds-settings&success=' . urlencode( __( 'Scraper workflow triggered successfully, but results may not be available yet. Click "Refresh Results" to check.', 'scraped-data-staging' ) ) ) );
            }
        } else {
            $body = json_decode( wp_remote_retrieve_body( $response ), true );
            wp_redirect( admin_url( 'edit.php?post_type=staging_scraped&page=sds-settings&error=' . urlencode( __( 'Failed to trigger scraper: ', 'scraped-data-staging' ) . ( $body['message'] ?? 'Unknown error' ) ) ) );
        }
        exit;
    }

    /**
     * Fetch GitHub Actions workflow results.
     */
    private static function get_workflow_results() {
        $token = get_option( 'sds_github_token', '' );
        $run_id = get_option( 'sds_last_workflow_run_id', '' );

        if ( ! $token || ! $run_id ) {
            return array( 'status' => 'error', 'message' => __( 'No workflow run ID or GitHub token available.', 'scraped-data-staging' ) );
        }

        $logs_url = "https://api.github.com/repos/" . self::$github_repo . "/actions/runs/$run_id/artifacts";
        $headers = array(
            'Authorization' => 'Bearer ' . $token,
            'Accept' => 'application/vnd.github.v3+json',
            'User-Agent' => 'WordPress-Scraped-Data-Staging',
        );

        // Fetch list of artifacts
        $response = wp_remote_get( $logs_url, array(
            'headers' => $headers,
            'timeout' => 15,
        ) );

        if ( is_wp_error( $response ) ) {
            return array( 'status' => 'error', 'message' => __( 'Failed to fetch artifacts list: ', 'scraped-data-staging' ) . $response->get_error_message() );
        }

        $status_code = wp_remote_retrieve_response_code( $response );
        if ( $status_code !== 200 ) {
            $body = json_decode( wp_remote_retrieve_body( $response ), true );
            return array( 'status' => 'error', 'message' => __( 'Failed to fetch artifacts list: ', 'scraped-data-staging' ) . ( $body['message'] ?? 'Unknown error' ) );
        }

        $artifacts = json_decode( wp_remote_retrieve_body( $response ), true );
        $artifact_id = null;
        foreach ( $artifacts['artifacts'] as $artifact ) {
            if ( $artifact['name'] === 'scraper-results' ) {
                $artifact_id = $artifact['id'];
                break;
            }
        }

        if ( ! $artifact_id ) {
            return array( 'status' => 'error', 'message' => __( 'No scraper-results artifact found. The scraper may still be running.', 'scraped-data-staging' ) );
        }

        // Fetch the artifact ZIP
        $artifact_url = "https://api.github.com/repos/" . self::$github_repo . "/actions/artifacts/$artifact_id/zip";
        $response = wp_remote_get( $artifact_url, array(
            'headers' => $headers,
            'timeout' => 15,
        ) );

        if ( is_wp_error( $response ) ) {
            return array( 'status' => 'error', 'message' => __( 'Failed to fetch artifact: ', 'scraped-data-staging' ) . $response->get_error_message() );
        }

        $status_code = wp_remote_retrieve_response_code( $response );
        if ( $status_code !== 200 ) {
            $body = json_decode( wp_remote_retrieve_body( $response ), true );
            return array( 'status' => 'error', 'message' => __( 'Failed to fetch artifact: ', 'scraped-data-staging' ) . ( $body['message'] ?? 'Unknown error' ) );
        }

        // Extract scrape_results.json from the ZIP
        $zip_content = wp_remote_retrieve_body( $response );
        $temp_file = wp_tempnam();
        file_put_contents( $temp_file, $zip_content );

        $results = array();
        if ( class_exists( 'ZipArchive' ) ) {
            $zip = new ZipArchive();
            if ( $zip->open( $temp_file ) === true ) {
                for ( $i = 0; $i < $zip->numFiles; $i++ ) {
                    $filename = $zip->getNameIndex( $i );
                    if ( strpos( $filename, 'scrape_results.json' ) !== false ) {
                        $results = json_decode( $zip->getFromIndex( $i ), true );
                        break;
                    }
                }
                $zip->close();
            }
        }

        wp_delete_file( $temp_file );

        if ( empty( $results ) ) {
            return array( 'status' => 'error', 'message' => __( 'No results found in the artifact. The scraper may still be running.', 'scraped-data-staging' ) );
        }

        return array( 'status' => 'success', 'results' => $results );
    }

    /**
     * Handle results refresh action.
     */
    public static function refresh_results() {
        if ( ! current_user_can( 'manage_options' ) ) {
            wp_die( __( 'Unauthorized access.', 'scraped-data-staging' ) );
        }

        check_admin_referer( 'sds_refresh_results_nonce' );

        wp_redirect( admin_url( 'edit.php?post_type=staging_scraped&page=sds-settings&results_refreshed=1' ) );
        exit;
    }

    /**
     * Render settings page with GitHub connection status, trigger button, and results table.
     */
    public static function settings_page_callback() {
        $connection = self::check_github_connection();
        $success = isset( $_GET['success'] ) ? sanitize_text_field( $_GET['success'] ) : '';
        $error = isset( $_GET['error'] ) ? sanitize_text_field( $_GET['error'] ) : '';
        $results_refreshed = isset( $_GET['results_refreshed'] ) ? true : false;
        $results = $results_refreshed ? self::get_workflow_results() : array( 'status' => 'none', 'message' => '' );
        ?>
        <div class="wrap">
            <h1><?php esc_html_e( 'Scraper Settings', 'scraped-data-staging' ); ?></h1>
            <?php if ( $success ) : ?>
                <div class="notice notice-success is-dismissible"><p><?php echo esc_html( $success ); ?></p></div>
            <?php endif; ?>
            <?php if ( $error ) : ?>
                <div class="notice notice-error is-dismissible"><p><?php echo esc_html( $error ); ?></p></div>
            <?php endif; ?>
            <h2><?php esc_html_e( 'GitHub Connection Status', 'scraped-data-staging' ); ?></h2>
            <p><strong><?php echo $connection['status'] === 'success' ? __( 'Connected', 'scraped-data-staging' ) : __( 'Not Connected', 'scraped-data-staging' ); ?></strong>: <?php echo esc_html( $connection['message'] ); ?></p>
            <form method="post" action="options.php">
                <?php
                settings_fields( 'sds_settings_group' );
                do_settings_sections( 'sds-settings' );
                submit_button();
                ?>
            </form>
            <h2><?php esc_html_e( 'Run Scraper', 'scraped-data-staging' ); ?></h2>
            <form method="post" action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>">
                <input type="hidden" name="action" value="sds_trigger_scraper">
                <?php wp_nonce_field( 'sds_trigger_scraper_nonce' ); ?>
                <p>
                    <input type="submit" class="button button-primary" value="<?php esc_attr_e( 'Run Scraper Now', 'scraped-data-staging' ); ?>" <?php echo $connection['status'] !== 'success' ? 'disabled' : ''; ?> />
                    <p class="description"><?php esc_html_e( 'Triggers the scraper script via GitHub Actions.', 'scraped-data-staging' ); ?></p>
                </p>
            </form>
            <h2><?php esc_html_e( 'Scraper Results', 'scraped-data-staging' ); ?></h2>
            <form method="post" action="<?php echo esc_url( admin_url( 'admin-post.php' ) ); ?>">
                <input type="hidden" name="action" value="sds_refresh_results">
                <?php wp_nonce_field( 'sds_refresh_results_nonce' ); ?>
                <p>
                    <input type="submit" class="button button-secondary" value="<?php esc_attr_e( 'Refresh Results', 'scraped-data-staging' ); ?>" <?php echo ! get_option( 'sds_last_workflow_run_id', '' ) ? 'disabled' : ''; ?> />
                    <p class="description"><?php esc_html_e( 'Fetches the latest results from the GitHub Actions workflow.', 'scraped-data-staging' ); ?></p>
                </p>
            </form>
            <?php if ( $results['status'] === 'success' && ! empty( $results['results'] ) ) : ?>
                <div class="sds-results">
                    <h3><?php esc_html_e( 'Latest Scraper Results', 'scraped-data-staging' ); ?></h3>
                    <table class="wp-list-table widefat fixed striped">
                        <thead>
                            <tr>
                                <th><?php esc_html_e( 'Type', 'scraped-data-staging' ); ?></th>
                                <th><?php esc_html_e( 'Job ID', 'scraped-data-staging' ); ?></th>
                                <th><?php esc_html_e( 'Job Title', 'scraped-data-staging' ); ?></th>
                                <th><?php esc_html_e( 'Company Name', 'scraped-data-staging' ); ?></th>
                                <th><?php esc_html_e( 'Post ID', 'scraped-data-staging' ); ?></th>
                                <th><?php esc_html_e( 'Status', 'scraped-data-staging' ); ?></th>
                                <th><?php esc_html_e( 'Error', 'scraped-data-staging' ); ?></th>
                            </tr>
                        </thead>
                        <tbody>
                            <?php foreach ( $results['results'] as $result ) : ?>
                                <tr>
                                    <td><?php echo esc_html( ucfirst( $result['type'] ) ); ?></td>
                                    <td><?php echo esc_html( $result['job_id'] ); ?></td>
                                    <td><?php echo esc_html( $result['job_title'] ); ?></td>
                                    <td><?php echo esc_html( $result['company_name'] ); ?></td>
                                    <td>
                                        <?php if ( $result['post_id'] && $result['status'] === 'success' ) : ?>
                                            <a href="<?php echo esc_url( admin_url( 'post.php?post=' . $result['post_id'] . '&action=edit' ) ); ?>">
                                                <?php echo esc_html( $result['post_id'] ); ?>
                                            </a>
                                        <?php else : ?>
                                            <?php echo esc_html( $result['post_id'] ); ?>
                                        <?php endif; ?>
                                    </td>
                                    <td><?php echo esc_html( ucfirst( $result['status'] ) ); ?></td>
                                    <td><?php echo esc_html( $result['error'] ); ?></td>
                                </tr>
                            <?php endforeach; ?>
                        </tbody>
                    </table>
                </div>
            <?php elseif ( $results['status'] === 'error' ) : ?>
                <div class="notice notice-error is-dismissible"><p><?php echo esc_html( $results['message'] ); ?></p></div>
            <?php elseif ( get_option( 'sds_last_workflow_run_id', '' ) ) : ?>
                <p><?php esc_html_e( 'Click "Refresh Results" to view the latest scraper results.', 'scraped-data-staging' ); ?></p>
            <?php else : ?>
                <p><?php esc_html_e( 'No results available. Run the scraper to generate results.', 'scraped-data-staging' ); ?></p>
            <?php endif; ?>
            <style>
                .sds-results table {
                    width: 100%;
                    border-collapse: collapse;
                    margin-top: 10px;
                }
                .sds-results th, .sds-results td {
                    padding: 8px;
                    text-align: left;
                    vertical-align: top;
                }
                .sds-results th {
                    background: #f5f5f5;
                    font-weight: bold;
                }
                .sds-results tr:nth-child(even) {
                    background: #f9f9f9;
                }
                .sds-results td {
                    max-width: 300px;
                    overflow: hidden;
                    text-overflow: ellipsis;
                    white-space: nowrap;
                }
                .sds-results td:hover {
                    overflow: visible;
                    white-space: normal;
                    word-break: break-word;
                }
            </style>
        </div>
        <?php
    }
}

// Initialize the plugin.
Scraped_Data_Staging::init();
